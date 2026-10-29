"""
Manga tracker - PoC
Search a series or a book (French editions only) and estimate how many
volumes the series has.

Sources:
  - Google Books API (works without a key but quota is tiny -> add a key)
  - BnF catalogue SRU API (free, no key, French legal deposit)

Run:  streamlit run app.py
Optional secret (.streamlit/secrets.toml or Streamlit Cloud secrets):
  GOOGLE_BOOKS_API_KEY = "xxxx"
"""
import re
import unicodedata
import xml.etree.ElementTree as ET

import pandas as pd
import requests
import streamlit as st

GOOGLE_URL = "https://www.googleapis.com/books/v1/volumes"
BNF_URL = "https://catalogue.bnf.fr/api/SRU"
NS = {
    "srw": "http://www.loc.gov/zing/srw/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

# Titles containing these words are not regular volumes of a series
EXCLUDE_WORDS = [
    "coffret", "box", "artbook", "art book", "databook", "data book",
    "guide", "magazine", "fanbook", "fan book", "anime comics", "roman",
    "light novel", "integrale", "intégrale", "pack", "poster",
]

VOLUME_PATTERNS = [
    re.compile(r"(?<!\w)(?:tome|vol(?:ume)?|t|n°|no|#)\.?\s*0*(\d{1,4})(?!\d)", re.I),
    re.compile(r"(?<!\d)0*(\d{1,4})\s*$"),
]
ISBN_RE = re.compile(r"97[89][\d\-\s]{10,14}\d|\b\d{9}[\dXx]\b")


# ----------------------------------------------------------------- helpers
def get_google_key():
    try:
        return st.secrets.get("GOOGLE_BOOKS_API_KEY")
    except Exception:
        return None


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def clean_isbn(raw: str) -> str:
    return re.sub(r"[^0-9Xx]", "", raw or "").upper()


def looks_like_isbn(text: str) -> bool:
    return len(clean_isbn(text)) in (10, 13) and not re.search(r"[A-WYZa-wyz]", text)


def extract_volume(title: str, series: str):
    """Return the volume number found in a title, ignoring digits that are
    part of the series name itself."""
    rest = re.sub(re.escape(series), " ", title, flags=re.I)
    for pattern in VOLUME_PATTERNS:
        m = pattern.search(rest)
        if m:
            return int(m.group(1))
    return None


def is_excluded(title: str) -> bool:
    t = normalize(title)
    return any(normalize(w) in t for w in EXCLUDE_WORDS)


# ------------------------------------------------------------- Google Books
@st.cache_data(ttl=3600, show_spinner=False)
def google_search(query: str, max_pages: int = 5):
    key = get_google_key()
    records = []
    for page in range(max_pages):
        params = {
            "q": query,
            "langRestrict": "fr",
            "printType": "books",
            "maxResults": 40,
            "startIndex": page * 40,
        }
        if key:
            params["key"] = key
        r = requests.get(GOOGLE_URL, params=params, timeout=15)
        if r.status_code == 429:
            raise RuntimeError(
                "Google Books quota exceeded. Add a free API key "
                "(GOOGLE_BOOKS_API_KEY) in your secrets."
            )
        r.raise_for_status()
        items = r.json().get("items", [])
        for it in items:
            info = it.get("volumeInfo", {})
            if info.get("language") != "fr":
                continue
            isbn = ""
            for ident in info.get("industryIdentifiers", []):
                if ident.get("type") == "ISBN_13":
                    isbn = ident.get("identifier", "")
            if not isbn:
                for ident in info.get("industryIdentifiers", []):
                    if ident.get("type") == "ISBN_10":
                        isbn = ident.get("identifier", "")
            title = info.get("title", "")
            if info.get("subtitle"):
                title = f"{title} - {info['subtitle']}"
            records.append(
                {
                    "source": "Google Books",
                    "title": title,
                    "authors": ", ".join(info.get("authors", [])),
                    "publisher": info.get("publisher", ""),
                    "date": info.get("publishedDate", ""),
                    "isbn": isbn,
                    "cover": (info.get("imageLinks", {}) or {})
                    .get("thumbnail", "")
                    .replace("http://", "https://"),
                }
            )
        if len(items) < 40:
            break
    return records


# --------------------------------------------------------------------- BnF
@st.cache_data(ttl=3600, show_spinner=False)
def bnf_search(cql_query: str, max_records: int = 200):
    records = []
    start = 1
    while start <= max_records:
        params = {
            "version": "1.2",
            "operation": "searchRetrieve",
            "query": cql_query,
            "recordSchema": "dublincore",
            "maximumRecords": 50,
            "startRecord": start,
        }
        r = requests.get(BNF_URL, params=params, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        recs = root.findall(".//srw:recordData/oai_dc:dc", NS)
        if not recs:
            break
        for dc in recs:
            def texts(tag):
                return [e.text.strip() for e in dc.findall(f"dc:{tag}", NS) if e.text]

            languages = texts("language")
            if languages and not any(l.lower().startswith("fre") for l in languages):
                continue
            isbn = ""
            for ident in texts("identifier"):
                m = ISBN_RE.search(ident)
                if m:
                    isbn = clean_isbn(m.group(0))
                    break
            titles = texts("title")
            records.append(
                {
                    "source": "BnF",
                    "title": titles[0] if titles else "",
                    "authors": ", ".join(texts("creator")),
                    "publisher": (texts("publisher") or [""])[0],
                    "date": (texts("date") or [""])[0],
                    "isbn": isbn,
                    "cover": "",
                }
            )
        if len(recs) < 50:
            break
        start += 50
    return records


def safe_call(label, func, *args):
    """Run a source; show a warning instead of crashing if it fails."""
    try:
        return func(*args)
    except Exception as exc:  # noqa: BLE001 - PoC
        st.warning(f"{label} unavailable: {exc}")
        return []


# ------------------------------------------------------------------ series
def build_series_volumes(series, author, use_bnf):
    g_query = f'intitle:"{series}"'
    if author:
        g_query += f' inauthor:"{author}"'
    records = safe_call("Google Books", google_search, g_query)

    if use_bnf:
        b_query = f'bib.title all "{series}"'
        if author:
            b_query += f' and bib.author all "{author}"'
        records += safe_call("BnF", bnf_search, b_query)

    series_norm = normalize(series)
    kept = []
    for rec in records:
        if series_norm not in normalize(rec["title"]):
            continue
        if is_excluded(rec["title"]):
            continue
        rec["volume"] = extract_volume(rec["title"], series)
        if rec["volume"] is None or not rec["isbn"]:
            continue
        kept.append(rec)
    return kept


def pick_best_per_volume(records):
    """One row per volume number; prefer a record with a cover, then Google."""
    best = {}
    for rec in records:
        v = rec["volume"]
        score = (bool(rec["cover"]), rec["source"] == "Google Books", rec["date"])
        if v not in best or score > best[v][0]:
            best[v] = (score, rec)
    return [best[v][1] for v in sorted(best)]


def render_series_tab():
    c1, c2 = st.columns([3, 2])
    series = c1.text_input("Series name", placeholder="One Piece", key="series_name")
    author = c2.text_input("Author (optional)", placeholder="Oda", key="series_author")
    use_bnf = st.checkbox("Also query the BnF catalogue", value=True)

    if not series.strip():
        st.info("Type a series name and press Enter.")
        return

    with st.spinner("Searching..."):
        records = build_series_volumes(series.strip(), author.strip(), use_bnf)

    if not records:
        st.error("No French volumes found. Try another spelling or remove the author.")
        return

    publishers = sorted({r["publisher"] for r in records if r["publisher"]})
    chosen = st.selectbox(
        "Publisher (several publishers can release the same series)",
        ["All"] + publishers,
    )
    if chosen != "All":
        records = [r for r in records if r["publisher"] == chosen]

    volumes = pick_best_per_volume(records)
    numbers = [v["volume"] for v in volumes]
    highest = max(numbers)
    missing = sorted(set(range(1, highest + 1)) - set(numbers))

    m1, m2, m3 = st.columns(3)
    m1.metric("Highest volume found", highest)
    m2.metric("Distinct volumes found", len(numbers))
    m3.metric("Gaps in catalogue data", len(missing))
    if missing:
        st.caption(
            "Volumes not found in the sources (probably a data gap, not a "
            f"missing volume): {', '.join(map(str, missing))}"
        )
    st.caption(
        "The count is an estimate: very recent or upcoming volumes may not be "
        "listed yet."
    )

    df = pd.DataFrame(volumes)[
        ["cover", "volume", "title", "authors", "publisher", "date", "isbn", "source"]
    ]
    st.dataframe(
        df,
        hide_index=True,
        use_container_width=True,
        column_config={"cover": st.column_config.ImageColumn("Cover", width="small")},
    )


# -------------------------------------------------------------------- book
def render_book_tab():
    query = st.text_input(
        "ISBN or book title", placeholder="9782723492324 or One Piece tome 1", key="book_q"
    )
    if not query.strip():
        st.info("Type an ISBN or a title and press Enter.")
        return

    query = query.strip()
    use_bnf = st.checkbox("Also query the BnF catalogue", value=True, key="book_bnf")
    with st.spinner("Searching..."):
        if looks_like_isbn(query):
            isbn = clean_isbn(query)
            records = safe_call("Google Books", google_search, f"isbn:{isbn}", 1)
            if use_bnf:
                records += safe_call("BnF", bnf_search, f'bib.isbn all "{isbn}"', 50)
        else:
            records = safe_call("Google Books", google_search, f'intitle:"{query}"', 1)
            if use_bnf:
                records += safe_call("BnF", bnf_search, f'bib.title all "{query}"', 50)

    # De-duplicate by ISBN (keep the record with a cover if possible)
    unique = {}
    for rec in records:
        key = rec["isbn"] or f"{rec['source']}:{rec['title']}"
        if key not in unique or (rec["cover"] and not unique[key]["cover"]):
            unique[key] = rec
    results = list(unique.values())[:20]

    if not results:
        st.error("No result.")
        return

    for rec in results:
        col_img, col_txt = st.columns([1, 5])
        if rec["cover"]:
            col_img.image(rec["cover"], width=90)
        col_txt.markdown(f"**{rec['title']}**")
        col_txt.write(
            f"{rec['authors'] or 'Unknown author'} · {rec['publisher'] or 'Unknown publisher'}"
            f" · {rec['date'] or 'n/a'}"
        )
        col_txt.caption(f"ISBN: {rec['isbn'] or 'n/a'} · source: {rec['source']}")
        st.divider()


# --------------------------------------------------------------------- app
st.set_page_config(page_title="Manga tracker PoC", page_icon="📚", layout="wide")
st.title("📚 Manga tracker - PoC")
st.caption("French editions only · Google Books + BnF")

if not get_google_key():
    st.sidebar.warning(
        "No GOOGLE_BOOKS_API_KEY found: Google Books requests will often hit "
        "the shared quota (HTTP 429). The BnF source still works."
    )

tab_series, tab_book = st.tabs(["Search a series", "Search a book"])
with tab_series:
    render_series_tab()
with tab_book:
    render_book_tab()
