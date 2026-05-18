"""
InsightsHub — multi-tool analytics app
"""

import io
import os
import re as _re
from typing import Optional

import pandas as pd
import streamlit as st

from normalizer import normalize, fingerprint_id


# ══════════════════════════════════════════════════════════════════════════════
# S3 helpers — module-level so every page can reuse them
# ══════════════════════════════════════════════════════════════════════════════

def _build_s3fs(key: str, secret: str, token: str):
    """Create an S3FileSystem, injecting explicit credentials only when present."""
    import s3fs as _s3fs
    kw: dict = {}
    if key:    kw["key"]    = key
    if secret: kw["secret"] = secret
    if token:  kw["token"]  = token
    return _s3fs.S3FileSystem(**kw) if kw else _s3fs.S3FileSystem(anon=False)


def _resolve_files(fs, prefix: str, pattern: str, fmt: str) -> list[str]:
    """
    Return a sorted list of s3:// paths under *prefix* that match *pattern*.

    pattern — glob (query_history*, *.parquet) OR Python regex OR empty string.
    Empty pattern → list everything directly in the folder (non-recursive).
    fmt     — "CSV" | "Parquet" | "Auto-detect"
    """
    bare = prefix.replace("s3://", "").rstrip("/")
    ext_map = {
        "CSV":     (".csv",),
        "Parquet": (".parquet", ".pq", ".parquet.snappy", ".parquet.gz",
                    ".parquet.zstd", ".parquet.brotli", ".parquet.lz4"),
    }
    exts = ext_map.get(fmt)  # None when Auto-detect → no extension filter

    def _keep(path: str) -> bool:
        if path.endswith("/"):
            return False
        # Auto-detect: accept every file regardless of extension
        if exts is None:
            return True
        return path.lower().endswith(exts)

    if not pattern:
        # detail=False → plain path strings (not dicts, which is the default in
        # s3fs 2024+ and causes AttributeError with .endswith())
        raw = fs.ls(bare, detail=False)
        return sorted("s3://" + f for f in raw if _keep(f))

    # 1. Glob first — handles *, ?.parquet, query_history*, 2024-*, etc.
    glob_hits = ["s3://" + f
                 for f in fs.glob(f"{bare}/{pattern}")
                 if _keep(f)]
    if glob_hits:
        return sorted(glob_hits)

    # 2. Fallback: treat pattern as Python regex (only when it compiles cleanly)
    try:
        rx = _re.compile(pattern)
    except _re.error:
        return []   # invalid as both glob and regex → nothing matched
    all_files = ["s3://" + f for f in fs.find(bare) if _keep(f)]
    return sorted(f for f in all_files if rx.search(f.split("/")[-1]))


def _read_s3_file(fpath: str, fmt: str, storage_options) -> pd.DataFrame:
    """
    Read one S3 file into a DataFrame.
    When fmt is "Auto-detect", CSV is used only when the extension is .csv;
    everything else (including extensionless Spark/Hive files) is read as Parquet.
    """
    use_csv = fmt == "CSV" or (
        fmt == "Auto-detect" and fpath.lower().endswith(".csv")
    )
    if use_csv:
        return pd.read_csv(fpath, storage_options=storage_options or None)
    return pd.read_parquet(fpath, storage_options=storage_options or None)


def _resolve_local_files(directory: str, pattern: str, fmt: str) -> list[str]:
    """
    Return a sorted list of local file paths under *directory* matching *pattern*.

    pattern — glob (query_history*, *.parquet, 2024-*) or empty string.
    Empty pattern → every file directly in the directory.
    fmt     — "CSV" | "Parquet" | "Auto-detect"
    """
    import glob as _glob

    ext_map = {
        "CSV":     (".csv",),
        "Parquet": (".parquet", ".pq"),
    }
    exts = ext_map.get(fmt)  # None when Auto-detect → accept all files

    def _keep(path: str) -> bool:
        if not os.path.isfile(path):
            return False
        if exts is None:
            return True
        return path.lower().endswith(exts)

    if not pattern:
        candidates = _glob.glob(os.path.join(directory, "*"))
    else:
        candidates = _glob.glob(os.path.join(directory, pattern))
        # If glob found nothing, also try a case-insensitive / recursive pass
        if not candidates:
            candidates = _glob.glob(os.path.join(directory, "**", pattern), recursive=True)

    return sorted(p for p in candidates if _keep(p))


def _read_local_file(fpath: str, fmt: str) -> pd.DataFrame:
    """Read a local file as a DataFrame, auto-detecting CSV vs Parquet."""
    use_csv = fmt == "CSV" or (fmt == "Auto-detect" and fpath.lower().endswith(".csv"))
    if use_csv:
        return pd.read_csv(fpath)
    return pd.read_parquet(fpath)


def _s3_error_hints(err: str):
    """Render helpful contextual hints beneath an S3 error message."""
    if any(k in err for k in ("ExpiredToken", "InvalidToken", "TokenRefreshRequired",
                               "Request has expired", "Token has expired")):
        st.warning(
            "⏱ **Your AWS session token has expired.** "
            "Generate new temporary credentials and paste them into the "
            "**S3 credentials** expander in the sidebar."
        )
    elif any(k in err for k in ("AccessDenied", "403", "Forbidden")):
        st.info(
            "🔒 **Access denied.** Check that your credentials have "
            "`s3:GetObject` and `s3:ListBucket` permission on this bucket."
        )
    elif any(k in err for k in ("InvalidClientTokenId", "AuthFailure",
                                 "NoCredentialProviders", "Unable to locate credentials")):
        st.info(
            "🔑 **Credentials missing or invalid.** Open the "
            "**S3 credentials** expander in the sidebar and enter fresh credentials."
        )
    elif "NoSuchBucket" in err or "NoSuchKey" in err:
        st.info("🪣 **Bucket or path not found.** Double-check the S3 path.")


# ── Shared output helper ──────────────────────────────────────────────────────
def _output_widget(df: pd.DataFrame, default_filename: str,
                   fmt: str, note: str, key_prefix: str,
                   aws_key: str, aws_secret: str, aws_token: str):
    """
    Renders a Download / Write to S3 output section.
    fmt: 'Parquet' or 'CSV'
    """
    st.markdown("**Save output**")
    dest = st.radio(
        "Output destination",
        ["Download locally", "Write to S3"],
        horizontal=True,
        label_visibility="collapsed",
        key=f"{key_prefix}_dest",
    )

    # Serialise once
    buf = io.BytesIO()
    if fmt == "Parquet":
        df.to_parquet(buf, index=False)
        mime     = "application/octet-stream"
        filename = default_filename if default_filename.endswith(".parquet") else default_filename + ".parquet"
    else:
        buf.write(df.to_csv(index=False).encode())
        mime     = "text/csv"
        filename = default_filename if default_filename.endswith(".csv") else default_filename + ".csv"
    file_bytes = buf.getvalue()

    if dest == "Download locally":
        st.download_button(
            f"⬇  Download {fmt}",
            data=file_bytes,
            file_name=filename,
            mime=mime,
            key=f"{key_prefix}_dl",
        )

    else:  # Write to S3
        s3_out = st.text_input(
            "S3 output path",
            placeholder="s3://my-bucket/output/results.parquet",
            key=f"{key_prefix}_s3out",
            help="Full S3 path including filename.",
        )
        if st.button("☁  Write to S3", type="primary", key=f"{key_prefix}_s3btn"):
            if not s3_out:
                st.warning("Enter an S3 output path first.")
            else:
                try:
                    import s3fs
                    kw = {}
                    if aws_key:    kw["key"]    = aws_key
                    if aws_secret: kw["secret"] = aws_secret
                    if aws_token:  kw["token"]  = aws_token
                    fs = s3fs.S3FileSystem(**kw) if kw else s3fs.S3FileSystem(anon=False)
                    with fs.open(s3_out, "wb") as f:
                        f.write(file_bytes)
                    st.success(f"Written → `{s3_out}`")
                except ImportError:
                    st.error("s3fs is not installed. Run: pip install s3fs")
                except Exception as e:
                    st.error(f"S3 write failed: {e}")

    if note:
        st.markdown(
            f'<p style="font-size:12px;color:#9CA3AF;margin-top:10px">{note}</p>',
            unsafe_allow_html=True,
        )

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="InsightsHub",
    page_icon="assets/favicon.png" if os.path.exists("assets/favicon.png") else "🔷",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Session state: active page ────────────────────────────────────────────────
if "page" not in st.session_state:
    st.session_state.page = "SQL Query Deduplicator"
if "df_raw" not in st.session_state:
    st.session_state.df_raw = None
if "dedup_result" not in st.session_state:
    st.session_state.dedup_result = None   # persists dedup output across reruns
if "hg_df" not in st.session_state:
    st.session_state.hg_df = None
if "hg_result" not in st.session_state:
    st.session_state.hg_result = None      # persists hash output across reruns

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
#MainMenu, footer { visibility: hidden; }

/* ── Sidebar logo ─────────────────────────────────────────────────────────── */
.sidebar-logo {
    display: flex; align-items: center; gap: 10px;
    padding: 18px 16px 14px 16px;
    border-bottom: 1px solid #E5E7EB;
    margin-bottom: 8px;
}
.sidebar-logo-icon {
    width: 32px; height: 32px; background: #5B3FD9; border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    color: white; font-size: 15px; font-weight: 700; flex-shrink: 0;
}
.sidebar-logo-text { font-size: 15px; font-weight: 700; letter-spacing: -0.3px; }

/* ── Nav items ────────────────────────────────────────────────────────────── */
.nav-item {
    display: flex; align-items: center; gap: 10px;
    padding: 9px 12px; margin: 2px 4px; border-radius: 6px;
    font-size: 13.5px; font-weight: 600; cursor: default;
}
.nav-item.active { background: #EDE9FF; color: #5B3FD9; }

/* Style sidebar buttons to look like nav items */
[data-testid="stSidebar"] .stButton > button {
    background: transparent !important;
    border: none !important;
    text-align: left !important;
    justify-content: flex-start !important;
    color: #374151 !important;
    font-size: 13.5px !important;
    font-weight: 500 !important;
    padding: 9px 12px !important;
    border-radius: 6px !important;
    box-shadow: none !important;
    width: 100% !important;
}
[data-testid="stSidebar"] .stButton > button:hover {
    background: #F3F4F6 !important;
    color: #111827 !important;
}

.sidebar-section {
    font-size: 11px; font-weight: 600; color: #9CA3AF;
    letter-spacing: 0.06em; text-transform: uppercase;
    padding: 14px 16px 4px 16px;
}

/* ── Page header ──────────────────────────────────────────────────────────── */
.page-header { margin-bottom: 20px; padding-bottom: 16px; border-bottom: 1px solid #E5E7EB; }
.page-header h1 { font-size: 22px; font-weight: 700; margin: 0 0 4px 0; letter-spacing: -0.4px; }
.page-header p  { font-size: 13.5px; color: #6B7280; margin: 0; }

/* ── Stats cards ──────────────────────────────────────────────────────────── */
.stats-row { display: flex; gap: 16px; margin-bottom: 20px; }
.stat-card {
    flex: 1; border: 1px solid #E5E7EB; border-radius: 8px; padding: 14px 18px;
    background: #FFFFFF;
}
.stat-label { font-size: 11.5px; font-weight: 500; color: #6B7280; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px; }
.stat-value { font-size: 26px; font-weight: 700; color: #111827; letter-spacing: -0.5px; }
.stat-value.purple { color: #5B3FD9; }
.stat-value.green  { color: #16A34A; }

/* ── Results header ───────────────────────────────────────────────────────── */
.results-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.results-title  { font-size: 14px; font-weight: 600; }
.results-count  { font-size: 12.5px; color: #6B7280; }

/* ── File list ────────────────────────────────────────────────────────────── */
.file-list-item {
    font-family: monospace; font-size: 12.5px; background: #F9FAFB;
    border: 1px solid #E5E7EB; border-radius: 5px; padding: 6px 12px; margin: 3px 0;
}

/* ── Coming soon ──────────────────────────────────────────────────────────── */
.coming-soon {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    min-height: 340px; color: #9CA3AF; text-align: center;
}
.coming-soon-icon { font-size: 40px; margin-bottom: 14px; }
.coming-soon h2   { font-size: 18px; font-weight: 600; color: #374151; margin: 0 0 6px 0; }
.coming-soon p    { font-size: 13.5px; margin: 0; }

/* ── Main content padding ─────────────────────────────────────────────────── */
.block-container { padding-top: 20px !important; max-width: 100% !important; }
</style>
""", unsafe_allow_html=True)

# ── Nav pages ─────────────────────────────────────────────────────────────────
PAGES = [
    {"id": "SQL Query Deduplicator"},
    {"id": "Hash Generator"},
]

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("""
    <div class="sidebar-logo">
        <div class="sidebar-logo-icon">I</div>
        <span class="sidebar-logo-text">InsightsHub</span>
    </div>
    <div class="sidebar-section">Tools</div>
    """, unsafe_allow_html=True)

    for p in PAGES:
        is_active = st.session_state.page == p["id"]
        if is_active:
            # Active: render as a styled highlight div (no button needed)
            st.markdown(
                f'<div class="nav-item active">{p["id"]}</div>',
                unsafe_allow_html=True,
            )
        else:
            # Inactive: real button styled via CSS to look like a nav item
            if st.button(p["id"], key=f"nav_{p['id']}",
                         use_container_width=True):
                st.session_state.page = p["id"]
                st.rerun()

    st.markdown('<hr>', unsafe_allow_html=True)

    st.markdown('<div class="sidebar-section">Settings</div>', unsafe_allow_html=True)

    try:
        _secrets = st.secrets.get("aws", {})
    except Exception:
        _secrets = {}

    with st.expander("S3 credentials"):
        st.markdown(
            '<p style="font-size:12px;color:#6B7280;margin:0 0 10px 0">'
            '<b>Running locally?</b> Leave blank — credentials are read from '
            '<code>~/.aws/credentials</code> or environment variables automatically.<br>'
            '<b>Deployed to Streamlit Cloud?</b> Add an <code>[aws]</code> section in '
            'your app\'s Secrets dashboard and leave these fields blank.'
            '</p>',
            unsafe_allow_html=True,
        )
        aws_key = st.text_input(
            "AWS_ACCESS_KEY_ID",
            value=_secrets.get("AWS_ACCESS_KEY_ID", ""),
            type="password",
            placeholder="AKIAxxxxxxxxxxxxxxxx",
            help="IAM user access key. Not needed if credentials are configured via secrets or env vars.",
        )
        aws_secret = st.text_input(
            "AWS_SECRET_ACCESS_KEY",
            value=_secrets.get("AWS_SECRET_ACCESS_KEY", ""),
            type="password",
            placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            help="IAM user secret key.",
        )
        aws_token = st.text_input(
            "AWS_SESSION_TOKEN",
            value=_secrets.get("AWS_SESSION_TOKEN", ""),
            type="password",
            placeholder="Optional — only for STS / AssumeRole / SSO temporary credentials",
            help="Only needed when using temporary credentials (STS AssumeRole, AWS SSO, aws-vault). Leave blank for plain IAM keys.",
        )
        st.markdown(
            '<p style="font-size:11px;color:#9CA3AF;margin:8px 0 0 0">'
            '🔒 Credentials entered here are held in memory for this browser session only and never written to disk.'
            '</p>',
            unsafe_allow_html=True,
        )

    # Credential status badge
    if aws_key and aws_secret:
        st.markdown(
            '<p style="font-size:11.5px;color:#16A34A;margin:6px 0 0 16px">● S3 credentials loaded</p>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<p style="font-size:11.5px;color:#F59E0B;margin:6px 0 0 16px">● No S3 credentials — using machine defaults</p>',
            unsafe_allow_html=True,
        )


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: SQL Query Deduplicator
# ══════════════════════════════════════════════════════════════════════════════
if st.session_state.page == "SQL Query Deduplicator":

    st.markdown("""
    <div class="page-header">
      <h1>SQL Query Deduplicator</h1>
      <p>Identify structurally identical queries that differ only in filter values — get back one runnable query per unique pattern.</p>
    </div>
    """, unsafe_allow_html=True)

    # ── Source selector ───────────────────────────────────────────────────────
    source = st.radio("Data source", ["Local file", "S3 path"], horizontal=True,
                      label_visibility="collapsed")
    st.markdown('<div style="height:4px"></div>', unsafe_allow_html=True)

    file_format = st.selectbox("Input format", ["Auto-detect", "CSV", "Parquet"])

    # ── Load data ─────────────────────────────────────────────────────────────
    # Reset stored data when source changes
    if "last_source" not in st.session_state:
        st.session_state.last_source = source
    if st.session_state.last_source != source:
        st.session_state.df_raw = None
        st.session_state.dedup_result = None
        st.session_state.last_source = source

    if source == "Local file":
        local_mode = st.radio(
            "local_mode",
            ["Upload file", "Local path"],
            horizontal=True,
            label_visibility="collapsed",
            key="dedup_local_mode",
        )

        # Reset data when sub-mode changes
        if "dedup_last_local_mode" not in st.session_state:
            st.session_state.dedup_last_local_mode = local_mode
        if st.session_state.dedup_last_local_mode != local_mode:
            st.session_state.df_raw = None
            st.session_state.dedup_result = None
            st.session_state.dedup_last_local_mode = local_mode

        if local_mode == "Upload file":
            uploaded = st.file_uploader(
                "Upload a CSV or Parquet file",
                type=["csv", "parquet", "pq"],
                label_visibility="collapsed",
            )
            if uploaded:
                fmt = file_format
                if fmt == "Auto-detect":
                    fmt = "Parquet" if uploaded.name.endswith((".parquet", ".pq")) else "CSV"
                try:
                    st.session_state.df_raw = (
                        pd.read_csv(uploaded) if fmt == "CSV" else pd.read_parquet(uploaded)
                    )
                    st.session_state.dedup_result = None
                except Exception as e:
                    st.error(f"Could not read file: {e}")

        else:  # Local path
            lc1, lc2 = st.columns([2, 1])
            with lc1:
                local_dir = st.text_input(
                    "Folder path",
                    placeholder="/path/to/your/folder",
                    help="Absolute path to the folder containing your data files.",
                    key="dedup_local_dir",
                )
            with lc2:
                local_pattern = st.text_input(
                    "File pattern",
                    placeholder="e.g. query_history*",
                    help="Glob pattern (query_history*, *.parquet, 2024-*). Leave blank to list all files.",
                    key="dedup_local_pattern",
                )

            lb1, lb2, _ = st.columns([1, 1, 4])
            local_list_btn = lb1.button("List files", type="secondary", key="dedup_local_list")
            local_load_btn = lb2.button("Load files", type="primary", key="dedup_local_load")

            if local_list_btn or local_load_btn:
                if not local_dir:
                    st.warning("Enter a folder path first.")
                elif not os.path.isdir(local_dir):
                    st.error(f"Folder not found: `{local_dir}`")
                else:
                    matched_local = _resolve_local_files(local_dir, local_pattern, file_format)
                    if not matched_local:
                        st.warning(
                            f"No files matched `{local_pattern or '*'}` in `{local_dir}`. "
                            "Check the path and pattern."
                        )
                    else:
                        st.markdown(
                            f'<div class="results-header">'
                            f'<span class="results-title">Matched files</span>'
                            f'<span class="results-count">'
                            f'{len(matched_local)} result{"s" if len(matched_local) != 1 else ""}'
                            f'</span></div>',
                            unsafe_allow_html=True,
                        )
                        for f in matched_local:
                            st.markdown(f'<div class="file-list-item">{f}</div>',
                                        unsafe_allow_html=True)
                        if local_load_btn:
                            try:
                                frames = []
                                prog = st.progress(0, text="Loading files…")
                                for i, fpath in enumerate(matched_local):
                                    prog.progress(
                                        (i + 1) / len(matched_local),
                                        text=f"Reading {os.path.basename(fpath)}  ({i+1}/{len(matched_local)})",
                                    )
                                    frames.append(_read_local_file(fpath, file_format))
                                st.session_state.df_raw = pd.concat(frames, ignore_index=True)
                                st.session_state.dedup_result = None
                                prog.empty()
                            except Exception as e:
                                st.error(f"Could not read files: {e}")

    else:  # S3
        c1, c2 = st.columns([2, 1])
        with c1:
            s3_prefix = st.text_input(
                "S3 path / prefix",
                placeholder="s3://my-bucket/queries/",
            )
        with c2:
            s3_pattern = st.text_input(
                "File pattern",
                placeholder="e.g. query_history*",
                help=(
                    "Glob (query_history*, *.parquet, 2024-*) or Python regex. "
                    "Leave blank to list all files in the folder."
                ),
            )

        btn_c1, btn_c2, _ = st.columns([1, 1, 4])
        list_btn = btn_c1.button("List files", type="secondary")
        load_s3  = btn_c2.button("Load from S3", type="primary")

        if list_btn or load_s3:
            if not s3_prefix:
                st.warning("Enter an S3 path first.")
            else:
                try:
                    fs  = _build_s3fs(aws_key, aws_secret, aws_token)
                    so  = {k: v for k, v in {"key": aws_key, "secret": aws_secret,
                                              "token": aws_token}.items() if v}
                    fmt = file_format   # "Auto-detect" | "CSV" | "Parquet"

                    matched_files = _resolve_files(fs, s3_prefix, s3_pattern, fmt)

                    if not matched_files:
                        st.warning(
                            f"No files matched `{s3_pattern or '*'}` under `{s3_prefix}`. "
                            "Check the path, pattern, and that your credentials have access."
                        )
                    else:
                        st.markdown(
                            f'<div class="results-header">'
                            f'<span class="results-title">Matched files</span>'
                            f'<span class="results-count">'
                            f'{len(matched_files)} result{"s" if len(matched_files) != 1 else ""}'
                            f'</span></div>',
                            unsafe_allow_html=True,
                        )
                        for f in matched_files:
                            st.markdown(f'<div class="file-list-item">{f}</div>',
                                        unsafe_allow_html=True)

                        if load_s3:
                            frames = []
                            prog = st.progress(0, text="Loading files…")
                            for i, fpath in enumerate(matched_files):
                                prog.progress(
                                    (i + 1) / len(matched_files),
                                    text=f"Reading {fpath.split('/')[-1]}  ({i+1}/{len(matched_files)})",
                                )
                                frames.append(_read_s3_file(fpath, fmt, so))
                            st.session_state.df_raw = pd.concat(frames, ignore_index=True)
                            st.session_state.dedup_result = None
                            prog.empty()

                except ImportError:
                    st.error("s3fs is not installed. Run: pip install s3fs")
                except Exception as e:
                    err = str(e)
                    st.error(f"S3 error: {err}")
                    _s3_error_hints(err)

    # ── Data loaded — column picker + run ─────────────────────────────────────
    df_raw = st.session_state.df_raw
    if df_raw is not None:

        info_c, clear_c = st.columns([8, 1])
        with clear_c:
            if st.button("✕ Clear", use_container_width=True):
                st.session_state.df_raw = None
                st.session_state.dedup_result = None
                st.rerun()
        with info_c:
            st.markdown(
                f'<div style="font-size:13px;color:#6B7280;padding-top:6px;">'
            f'<b style="color:#111827">{len(df_raw):,}</b> rows &nbsp;·&nbsp; '
            f'<b style="color:#111827">{df_raw.shape[1]}</b> columns &nbsp;·&nbsp; '
            f'Columns: {", ".join(f"<code>{c}</code>" for c in df_raw.columns)}'
            f'</div>',
            unsafe_allow_html=True,
        )

        with st.expander("Preview data (first 5 rows)", expanded=False):
            st.dataframe(df_raw.head(5), use_container_width=True)

        st.markdown("**Which column contains the SQL queries?**")
        sel_c1, sel_c2, sel_c3 = st.columns([2, 2, 2])

        with sel_c1:
            query_col = st.selectbox(
                "SQL query column",
                options=list(df_raw.columns),
                help="Select the column that contains the SQL query text to be analysed.",
                label_visibility="collapsed",
            )

        with sel_c2:
            st.markdown(
                f'<div style="padding-top:8px;font-size:12.5px;color:#6B7280;">'
                f'dtype: <b>{df_raw[query_col].dtype}</b> &nbsp;·&nbsp; '
                f'{df_raw[query_col].notna().sum():,} non-null values</div>',
                unsafe_allow_html=True,
            )

        with sel_c3:
            run_btn = st.button("▶  Run analysis", type="primary", use_container_width=True)

        sample_vals = df_raw[query_col].dropna().astype(str).head(3).tolist()
        with st.expander(f"Preview: first 3 values from '{query_col}'", expanded=True):
            for v in sample_vals:
                st.code(v, language="sql")

        st.markdown('<hr style="margin:12px 0 20px 0">', unsafe_allow_html=True)

        # Clear saved result when user changes the query column selection
        if (st.session_state.dedup_result is not None and
                st.session_state.dedup_result.get("query_col") != query_col):
            st.session_state.dedup_result = None

        if run_btn:
            queries = df_raw[query_col].dropna().astype(str)
            total   = len(queries)

            prog = st.progress(0, text="Normalising queries…")

            work_df = df_raw.copy()
            norms, fids = [], []
            for i, q in enumerate(df_raw[query_col].fillna("").astype(str)):
                norm = normalize(q)
                norms.append(norm)
                fids.append(fingerprint_id(norm))
                if i % 500 == 0:
                    prog.progress(min(i / total, 1.0),
                                  text=f"Normalising…  {i:,} / {total:,}")

            work_df["_pattern"]    = norms
            work_df["_pattern_id"] = fids
            prog.progress(1.0, text="Grouping…")
            work_df["_pattern_count"] = work_df.groupby("_pattern_id")["_pattern_id"].transform("count")

            result_df = (
                work_df
                .sort_values(["_pattern_count", "_pattern_id"], ascending=[False, True])
                .reset_index(drop=True)
            )

            dedup_df = (
                result_df
                .groupby("_pattern_id", sort=False)[query_col]
                .first()
                .reset_index(drop=True)
                .to_frame(name=query_col)
            )

            unique_patterns = work_df["_pattern_id"].nunique()
            prog.empty()

            # Persist so the output section survives reruns triggered by
            # widget interactions (e.g. switching to "Write to S3")
            st.session_state.dedup_result = {
                "dedup_df":       dedup_df,
                "total":          total,
                "unique_patterns": unique_patterns,
                "query_col":      query_col,
            }

        # ── Render results (from state — survives any rerun) ──────────────────
        if st.session_state.dedup_result is not None:
            res          = st.session_state.dedup_result
            dedup_df     = res["dedup_df"]
            total        = res["total"]
            unique_patterns = res["unique_patterns"]
            query_col    = res["query_col"]

            st.markdown(f"""
            <div class="stats-row">
              <div class="stat-card">
                <div class="stat-label">Total queries</div>
                <div class="stat-value">{total:,}</div>
              </div>
              <div class="stat-card">
                <div class="stat-label">Unique patterns</div>
                <div class="stat-value purple">{unique_patterns:,}</div>
              </div>
              <div class="stat-card">
                <div class="stat-label">Avg queries / pattern</div>
                <div class="stat-value">{total / unique_patterns:.1f}</div>
              </div>
              <div class="stat-card">
                <div class="stat-label">Reduction potential</div>
                <div class="stat-value green">{(1 - unique_patterns/total)*100:.1f}%</div>
              </div>
            </div>
            """, unsafe_allow_html=True)

            st.markdown(
                f'<div class="results-header">'
                f'<span class="results-title">Unique queries</span>'
                f'<span class="results-count">{total:,} input → {len(dedup_df):,} unique patterns</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

            st.dataframe(
                dedup_df,
                use_container_width=True,
                height=460,
                column_config={
                    query_col: st.column_config.TextColumn(query_col, width="large"),
                },
            )

            st.markdown('<hr style="margin:16px 0">', unsafe_allow_html=True)
            _output_widget(
                df=dedup_df,
                default_filename="unique_queries.parquet",
                fmt="Parquet",
                note="Output is the original query text — comments, casing, and filter values are untouched. Parquet is used to avoid CSV cell character limits.",
                key_prefix="dedup",
                aws_key=aws_key, aws_secret=aws_secret, aws_token=aws_token,
            )


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: Hash Generator
# ══════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "Hash Generator":
    import hashlib
    import re as _re

    st.markdown("""
    <div class="page-header">
      <h1>Hash Generator</h1>
      <p>Generate SHA-256 fingerprints for SQL queries and embed them as comments — making every query uniquely trackable.</p>
    </div>
    """, unsafe_allow_html=True)

    # ── Helper functions ──────────────────────────────────────────────────────
    def _hash_query(query: str) -> Optional[str]:
        if not isinstance(query, str):
            return None
        normalized = _re.sub(r"\s+", "", query)
        return hashlib.sha256(normalized.encode()).hexdigest()

    def _embed_hash_comment(query: str, company: str) -> str:
        h = _hash_query(query)
        if h is None:
            return query
        return f"/* {company}::{h} */\n{query}"

    # ── Config ────────────────────────────────────────────────────────────────
    st.markdown("**Configuration**")
    cfg_c1, cfg_c2 = st.columns([2, 4])
    with cfg_c1:
        company_name = st.text_input(
            "Company / namespace",
            value="company",
            help="Prefix embedded in the hash comment: /* company::hash */",
        )

    st.markdown('<div style="height:4px"></div>', unsafe_allow_html=True)

    # ── Data source ───────────────────────────────────────────────────────────
    st.markdown("**Data source**")
    hg_source = st.radio("Source", ["Local file", "S3 path"], horizontal=True,
                         label_visibility="collapsed", key="hg_source")

    hg_fmt = st.selectbox("Input format", ["Auto-detect", "CSV", "Parquet"], key="hg_fmt")

    if "hg_last_source" not in st.session_state:
        st.session_state.hg_last_source = hg_source
    if st.session_state.hg_last_source != hg_source:
        st.session_state.hg_df = None
        st.session_state.hg_result = None
        st.session_state.hg_last_source = hg_source

    if hg_source == "Local file":
        hg_local_mode = st.radio(
            "hg_local_mode",
            ["Upload file", "Local path"],
            horizontal=True,
            label_visibility="collapsed",
            key="hg_local_mode",
        )

        # Reset data when sub-mode changes
        if "hg_last_local_mode" not in st.session_state:
            st.session_state.hg_last_local_mode = hg_local_mode
        if st.session_state.hg_last_local_mode != hg_local_mode:
            st.session_state.hg_df = None
            st.session_state.hg_result = None
            st.session_state.hg_last_local_mode = hg_local_mode

        if hg_local_mode == "Upload file":
            hg_uploaded = st.file_uploader(
                "Upload a CSV or Parquet file",
                type=["csv", "parquet", "pq"],
                key="hg_upload",
            )
            if hg_uploaded:
                fmt = hg_fmt
                if fmt == "Auto-detect":
                    fmt = "Parquet" if hg_uploaded.name.endswith((".parquet", ".pq")) else "CSV"
                try:
                    st.session_state.hg_df = (
                        pd.read_csv(hg_uploaded) if fmt == "CSV" else pd.read_parquet(hg_uploaded)
                    )
                    st.session_state.hg_result = None
                except Exception as e:
                    st.error(f"Could not read file: {e}")

        else:  # Local path
            hg_lc1, hg_lc2 = st.columns([2, 1])
            with hg_lc1:
                hg_local_dir = st.text_input(
                    "Folder path",
                    placeholder="/path/to/your/folder",
                    help="Absolute path to the folder containing your data files.",
                    key="hg_local_dir",
                )
            with hg_lc2:
                hg_local_pattern = st.text_input(
                    "File pattern",
                    placeholder="e.g. *.parquet",
                    help="Glob pattern (*.parquet, query_history*, 2024-*). Leave blank to list all files.",
                    key="hg_local_pattern",
                )

            hg_lb1, hg_lb2, _ = st.columns([1, 1, 4])
            hg_local_list_btn = hg_lb1.button("List files", type="secondary", key="hg_local_list")
            hg_local_load_btn = hg_lb2.button("Load files", type="primary", key="hg_local_load")

            if hg_local_list_btn or hg_local_load_btn:
                if not hg_local_dir:
                    st.warning("Enter a folder path first.")
                elif not os.path.isdir(hg_local_dir):
                    st.error(f"Folder not found: `{hg_local_dir}`")
                else:
                    hg_matched_local = _resolve_local_files(hg_local_dir, hg_local_pattern, hg_fmt)
                    if not hg_matched_local:
                        st.warning(
                            f"No files matched `{hg_local_pattern or '*'}` in `{hg_local_dir}`. "
                            "Check the path and pattern."
                        )
                    else:
                        st.markdown(
                            f'<div class="results-header">'
                            f'<span class="results-title">Matched files</span>'
                            f'<span class="results-count">'
                            f'{len(hg_matched_local)} result{"s" if len(hg_matched_local) != 1 else ""}'
                            f'</span></div>',
                            unsafe_allow_html=True,
                        )
                        for f in hg_matched_local:
                            st.markdown(f'<div class="file-list-item">{f}</div>',
                                        unsafe_allow_html=True)
                        if hg_local_load_btn:
                            try:
                                frames = []
                                prog = st.progress(0, text="Loading files…")
                                for i, fpath in enumerate(hg_matched_local):
                                    prog.progress(
                                        (i + 1) / len(hg_matched_local),
                                        text=f"Reading {os.path.basename(fpath)}  ({i+1}/{len(hg_matched_local)})",
                                    )
                                    frames.append(_read_local_file(fpath, hg_fmt))
                                st.session_state.hg_df = pd.concat(frames, ignore_index=True)
                                st.session_state.hg_result = None
                                prog.empty()
                            except Exception as e:
                                st.error(f"Could not read files: {e}")

    else:  # S3
        hg_c1, hg_c2 = st.columns([2, 1])
        with hg_c1:
            hg_s3_prefix = st.text_input(
                "S3 path / prefix",
                placeholder="s3://my-bucket/queries/",
                key="hg_s3_prefix",
            )
        with hg_c2:
            hg_s3_pattern = st.text_input(
                "File pattern",
                placeholder="*.parquet",
                help=(
                    "Glob (*.parquet, 2024-*) or Python regex. "
                    "Leave blank to list all files in the folder."
                ),
                key="hg_s3_pattern",
            )

        hg_btn_c1, hg_btn_c2, _ = st.columns([1, 1, 4])
        hg_list_btn = hg_btn_c1.button("List files", type="secondary", key="hg_list")
        hg_load_btn = hg_btn_c2.button("Load from S3", type="primary", key="hg_load")

        if hg_list_btn or hg_load_btn:
            if not hg_s3_prefix:
                st.warning("Enter an S3 path first.")
            else:
                try:
                    fs  = _build_s3fs(aws_key, aws_secret, aws_token)
                    so  = {k: v for k, v in {"key": aws_key, "secret": aws_secret,
                                              "token": aws_token}.items() if v}
                    fmt = hg_fmt   # "Auto-detect" | "CSV" | "Parquet"

                    matched = _resolve_files(fs, hg_s3_prefix, hg_s3_pattern, fmt)

                    if not matched:
                        st.warning(
                            f"No files matched `{hg_s3_pattern or '*'}` under `{hg_s3_prefix}`. "
                            "Check the path, pattern, and that your credentials have access."
                        )
                    else:
                        st.markdown(
                            f'<div class="results-header">'
                            f'<span class="results-title">Matched files</span>'
                            f'<span class="results-count">'
                            f'{len(matched)} result{"s" if len(matched) != 1 else ""}'
                            f'</span></div>',
                            unsafe_allow_html=True,
                        )
                        for f in matched:
                            st.markdown(f'<div class="file-list-item">{f}</div>',
                                        unsafe_allow_html=True)

                        if hg_load_btn:
                            frames = []
                            prog = st.progress(0, text="Loading files…")
                            for i, fpath in enumerate(matched):
                                prog.progress(
                                    (i + 1) / len(matched),
                                    text=f"Reading {fpath.split('/')[-1]}  ({i+1}/{len(matched)})",
                                )
                                frames.append(_read_s3_file(fpath, fmt, so))
                            st.session_state.hg_df = pd.concat(frames, ignore_index=True)
                            st.session_state.hg_result = None
                            prog.empty()

                except ImportError:
                    st.error("s3fs is not installed. Run: pip install s3fs")
                except Exception as e:
                    err = str(e)
                    st.error(f"S3 error: {err}")
                    _s3_error_hints(err)

    hg_df = st.session_state.hg_df

    if hg_df is not None:

        info_c, clear_c = st.columns([8, 1])
        with clear_c:
            if st.button("✕ Clear", key="hg_clear", use_container_width=True):
                st.session_state.hg_df = None
                st.session_state.hg_result = None
                st.rerun()
        with info_c:
            st.markdown(
                f'<div style="font-size:13px;color:#6B7280;padding-top:6px;">'
                f'<b style="color:#111827">{len(hg_df):,}</b> rows &nbsp;·&nbsp; '
                f'<b style="color:#111827">{hg_df.shape[1]}</b> columns &nbsp;·&nbsp; '
                f'Columns: {", ".join(f"<code>{c}</code>" for c in hg_df.columns)}'
                f'</div>',
                unsafe_allow_html=True,
            )

        with st.expander("Preview data (first 5 rows)", expanded=False):
            st.dataframe(hg_df.head(5), use_container_width=True)

        # ── Column + output config ────────────────────────────────────────────
        st.markdown("**Which column contains the SQL queries?**")
        col_c1, col_c2, col_c3 = st.columns([2, 2, 2])

        with col_c1:
            hg_query_col = st.selectbox(
                "SQL query column",
                options=list(hg_df.columns),
                label_visibility="collapsed",
                key="hg_query_col",
            )
        with col_c2:
            hg_hash_col = st.text_input(
                "Hash column name",
                value="query_hash",
                help="Name of the new column that will store the SHA-256 hash.",
                key="hg_hash_col",
            )
        with col_c3:
            hg_out_fmt = st.selectbox(
                "Output format",
                ["Parquet", "CSV"],
                key="hg_out_fmt",
                help="Parquet recommended for large files — no cell character limits.",
            )

        sample_vals = hg_df[hg_query_col].dropna().astype(str).head(2).tolist()
        with st.expander(f"Preview: first 2 values from '{hg_query_col}'", expanded=True):
            for v in sample_vals:
                st.code(v, language="sql")

        hashed_col = f"{hg_query_col}_hashed"
        st.markdown(
            f'<p style="font-size:12.5px;color:#6B7280;margin:8px 0 16px 0">'
            f'Two new columns will be added — <code>{hashed_col}</code> (original query with '
            f'<code>/* {company_name or "company"}::&lt;sha256&gt; */</code> prepended) and '
            f'<code>{hg_hash_col}</code> (bare SHA-256 hex). '
            f'The original <code>{hg_query_col}</code> column is left untouched. '
            f'Any existing comments in the query are preserved.</p>',
            unsafe_allow_html=True,
        )

        st.markdown('<hr style="margin:4px 0 16px 0">', unsafe_allow_html=True)

        # Clear saved result when user changes query column or hash column name
        if st.session_state.hg_result is not None and (
            st.session_state.hg_result.get("query_col") != hg_query_col or
            st.session_state.hg_result.get("hash_col")  != hg_hash_col
        ):
            st.session_state.hg_result = None

        hg_run = st.button("▶  Generate hashes", type="primary")

        if hg_run:
            company    = company_name.strip() or "company"
            hashed_col = f"{hg_query_col}_hashed"
            prog = st.progress(0, text="Generating hashes…")

            out_df = hg_df.copy()
            hashes, commented = [], []
            total = len(out_df)

            for i, q in enumerate(out_df[hg_query_col].astype(str)):
                hashes.append(_hash_query(q))
                commented.append(_embed_hash_comment(q, company))
                if i % 500 == 0:
                    prog.progress(min(i / total, 1.0), text=f"Hashing…  {i:,} / {total:,}")

            # Original column is never modified — two new columns are added
            out_df[hg_hash_col] = hashes
            out_df[hashed_col]  = commented
            prog.progress(1.0, text="Done")
            prog.empty()

            # Persist so output section survives reruns (e.g. switching to "Write to S3")
            st.session_state.hg_result = {
                "out_df":     out_df,
                "total":      total,
                "company":    company,
                "query_col":  hg_query_col,
                "hash_col":   hg_hash_col,
                "hashed_col": hashed_col,
                "out_fmt":    hg_out_fmt,
            }

        # ── Render results (from state — survives any rerun) ──────────────────
        if st.session_state.hg_result is not None:
            res        = st.session_state.hg_result
            out_df     = res["out_df"]
            total      = res["total"]
            company    = res["company"]
            hashed_col = res["hashed_col"]
            _hash_col  = res["hash_col"]
            _out_fmt   = res["out_fmt"]

            unique_hashes = out_df[_hash_col].nunique()
            null_count    = out_df[_hash_col].isna().sum()

            st.markdown(f"""
            <div class="stats-row">
              <div class="stat-card">
                <div class="stat-label">Total rows</div>
                <div class="stat-value">{total:,}</div>
              </div>
              <div class="stat-card">
                <div class="stat-label">Unique hashes</div>
                <div class="stat-value purple">{unique_hashes:,}</div>
              </div>
              <div class="stat-card">
                <div class="stat-label">Duplicate queries</div>
                <div class="stat-value">{total - unique_hashes:,}</div>
              </div>
              <div class="stat-card">
                <div class="stat-label">Null / skipped</div>
                <div class="stat-value">{null_count:,}</div>
              </div>
            </div>
            """, unsafe_allow_html=True)

            st.markdown(
                f'<div class="results-header">'
                f'<span class="results-title">Output preview</span>'
                f'<span class="results-count">first 5 rows</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
            st.dataframe(out_df.head(5), use_container_width=True)

            st.markdown('<hr style="margin:16px 0">', unsafe_allow_html=True)
            _output_widget(
                df=out_df,
                default_filename="hashed_queries",
                fmt=_out_fmt,
                note=f"Hash format: <code>/* {company}::&lt;sha256&gt; */</code> — whitespace is stripped before hashing so formatting differences do not affect the hash.",
                key_prefix="hg",
                aws_key=aws_key, aws_secret=aws_secret, aws_token=aws_token,
            )
