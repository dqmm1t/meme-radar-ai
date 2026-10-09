
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st

# -----------------------------
# PAGE SETTINGS
# -----------------------------
st.set_page_config(
    page_title="Meme Radar AI",
    page_icon="🛰️",
    layout="wide",
)

API = "https://api.dexscreener.com"
MAX_TOKENS = 12
REQUEST_TIMEOUT = 8


# -----------------------------
# API HELPERS
# -----------------------------
def fetch_json(url):
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


@st.cache_data(ttl=60, show_spinner=False)
def get_profiles():
    data = fetch_json(f"{API}/token-profiles/latest/v1")

    if not isinstance(data, list):
        return []

    profiles = []
    seen = set()

    for item in data:
        if item.get("chainId") != "solana":
            continue

        address = item.get("tokenAddress")

        if not address or address in seen:
            continue

        seen.add(address)
        profiles.append(item)

        if len(profiles) >= MAX_TOKENS:
            break

    return profiles


@st.cache_data(ttl=60, show_spinner=False)
def get_pairs(address):
    data = fetch_json(f"{API}/token-pairs/v1/solana/{address}")
    return data if isinstance(data, list) else []


def number(value, default=0.0):
    try:
        result = float(value)
        if pd.notna(result):
            return result
    except (TypeError, ValueError):
        pass

    return default


# -----------------------------
# TOKEN SCORING
# -----------------------------
def score_token(liquidity, volume, change, buys, sells, age_hours):
    score = 0

    # Liquidity
    if liquidity >= 5_000:
        score += 15
    if liquidity >= 20_000:
        score += 10

    # Trading volume
    if volume >= 5_000:
        score += 10
    if volume >= 25_000:
        score += 10

    # Buy activity
    total = buys + sells
    if total > 0:
        buy_ratio = buys / total

        if buy_ratio >= 0.55:
            score += 15
        if buy_ratio >= 0.65:
            score += 10

    # Recent pair creation
    if age_hours is not None and 0 <= age_hours <= 24:
        score += 10

    # Price movement
    if 0 < change <= 20:
        score += 10
    elif 20 < change <= 60:
        score += 15
    elif change > 60:
        score += 5

    # Risk penalties
    if liquidity < 2_000:
        score -= 25
    if volume <= 0:
        score -= 10
    if change < -20:
        score -= 10

    return max(0, min(100, score))


# -----------------------------
# FETCH ONE TOKEN
# -----------------------------
def analyze_token(profile):
    address = profile["tokenAddress"]
    pairs = get_pairs(address)

    if not pairs:
        return None

    # Select the pair with the highest reported liquidity.
    pair = max(
        pairs,
        key=lambda p: number(
            (p.get("liquidity") or {}).get("usd")
        ),
    )

    base = pair.get("baseToken") or {}
    liquidity = number(
        (pair.get("liquidity") or {}).get("usd")
    )
    volume = number(
        (pair.get("volume") or {}).get("h24")
    )
    change = number(
        (pair.get("priceChange") or {}).get("h24")
    )

    transactions = (
        (pair.get("txns") or {}).get("h24") or {}
    )

    buys = int(number(transactions.get("buys")))
    sells = int(number(transactions.get("sells")))

    created = pair.get("pairCreatedAt")
    age_hours = None

    if created:
        age_hours = max(
            0,
            (time.time() * 1000 - number(created)) / 3_600_000,
        )

    score = score_token(
        liquidity,
        volume,
        change,
        buys,
        sells,
        age_hours,
    )

    return {
        "Token": base.get("name") or "Unknown",
        "Symbol": base.get("symbol") or "?",
        "Price USD": number(pair.get("priceUsd")),
        "Market Cap USD": number(
            pair.get("marketCap") or pair.get("fdv")
        ),
        "Liquidity USD": liquidity,
        "Volume 24h USD": volume,
        "Change 24h %": change,
        "Buys": buys,
        "Sells": sells,
        "Age Hours": round(age_hours, 1)
        if age_hours is not None else None,
        "Score": score,
        "Pair URL": pair.get("url", ""),
        "Address": address,
    }


# -----------------------------
# SCANNER
# -----------------------------
def scan_tokens():
    profiles = get_profiles()

    if not profiles:
        return [], 0

    results = []
    errors = 0

    progress = st.progress(0, text="Scanning Solana tokens...")

    # Run a few requests at the same time to reduce waiting.
    with ThreadPoolExecutor(max_workers=4) as executor:
        tasks = {
            executor.submit(analyze_token, profile): profile
            for profile in profiles
        }

        completed = 0

        for task in as_completed(tasks):
            completed += 1

            try:
                row = task.result()
                if row:
                    results.append(row)
            except Exception:
                errors += 1

            progress.progress(
                completed / len(tasks),
                text=f"Checked {completed} of {len(tasks)} tokens",
            )

    progress.empty()
    return results, errors


# -----------------------------
# WEBSITE
# -----------------------------
st.title("🛰️ Meme Radar AI")
st.caption("Solana token discovery and momentum scanner")

st.warning(
    "Scores use experimental rules, not predictive AI. "
    "Market data might be incomplete or manipulated. "
    "A high score does not mean a token is safe or profitable."
)

with st.sidebar:
    st.header("Scanner settings")

    min_liquidity = st.number_input(
        "Minimum liquidity ($)",
        min_value=0,
        value=5_000,
        step=1_000,
    )

    min_volume = st.number_input(
        "Minimum 24h volume ($)",
        min_value=0,
        value=5_000,
        step=1_000,
    )

    max_age = st.number_input(
        "Maximum pair age (hours)",
        min_value=1,
        value=72,
        step=12,
    )

    refresh = st.button(
        "Refresh market data",
        use_container_width=True,
    )

if refresh:
    get_profiles.clear()
    get_pairs.clear()
    st.session_state.pop("scan_rows", None)

if "scan_rows" not in st.session_state:
    try:
        with st.spinner("Connecting to market data..."):
            rows, errors = scan_tokens()

        st.session_state["scan_rows"] = rows
        st.session_state["scan_errors"] = errors

    except requests.RequestException as exc:
        st.error(
            "Couldn't reach DEX Screener. Check your connection "
            "and try again."
        )
        st.caption(f"Technical details: {exc}")
        st.stop()

rows = st.session_state["scan_rows"]
errors = st.session_state.get("scan_errors", 0)

if not rows:
    st.warning(
        "No token pairs loaded. Try refreshing in a minute. "
        "The public API might be temporarily unavailable."
    )
    st.stop()

df = pd.DataFrame(rows)

filtered = df[
    (df["Liquidity USD"] >= min_liquidity)
    & (df["Volume 24h USD"] >= min_volume)
    & (
        df["Age Hours"].isna()
        | (df["Age Hours"] <= max_age)
    )
].copy()

filtered = filtered.sort_values(
    ["Score", "Volume 24h USD"],
    ascending=False,
).reset_index(drop=True)

# Summary metrics
a, b, c, d = st.columns(4)

a.metric("Tokens loaded", len(df))
b.metric("Passed filters", len(filtered))
c.metric(
    "Top score",
    int(filtered["Score"].max()) if not filtered.empty else "None",
)
d.metric("Request errors", errors)

# Main results
st.subheader("Early Momentum Watchlist")

if filtered.empty:
    st.info(
        "No tokens meet your current filters. "
        "Lower the minimum liquidity or volume."
    )
else:
    display = filtered.rename(
        columns={
            "Price USD": "Price ($)",
            "Market Cap USD": "Market Cap ($)",
            "Liquidity USD": "Liquidity ($)",
            "Volume 24h USD": "Volume 24h ($)",
            "Change 24h %": "Change 24h (%)",
            "Age Hours": "Pair Age (hours)",
        }
    )

    st.dataframe(
        display[
            [
                "Token",
                "Symbol",
                "Score",
                "Price ($)",
                "Market Cap ($)",
                "Liquidity ($)",
                "Volume 24h ($)",
                "Change 24h (%)",
                "Pair Age (hours)",
                "Buys",
                "Sells",
                "Pair URL",
                "Address",
            ]
        ],
        use_container_width=True,
        hide_index=True,
        column_config={
            "Pair URL": st.column_config.LinkColumn(
                "DEX Screener"
            ),
            "Price ($)": st.column_config.NumberColumn(
                format="%.10f"
            ),
            "Market Cap ($)": st.column_config.NumberColumn(
                format="$%.0f"
            ),
            "Liquidity ($)": st.column_config.NumberColumn(
                format="$%.0f"
            ),
            "Volume 24h ($)": st.column_config.NumberColumn(
                format="$%.0f"
            ),
            "Change 24h (%)": st.column_config.NumberColumn(
                format="%.2f%%"
            ),
        },
    )

    st.download_button(
        "Download watchlist CSV",
        data=filtered.to_csv(index=False).encode("utf-8"),
        file_name="meme_radar_watchlist.csv",
        mime="text/csv",
    )

# Token details
st.subheader("Token Details")

selected = st.selectbox(
    "Choose a token",
    options=range(len(df)),
    format_func=lambda idx: (
        f"{df.iloc[idx]['Symbol']} | "
        f"{df.iloc[idx]['Token']} | "
        f"Score {df.iloc[idx]['Score']}"
    ),
)

token = df.iloc[selected]

left, right = st.columns(2)

with left:
    st.write(f"**Name:** {token['Token']}")
    st.write(f"**Symbol:** {token['Symbol']}")
    st.write(f"**Price:** ${token['Price USD']:.10f}")
    st.write(f"**Liquidity:** ${token['Liquidity USD']:,.0f}")
    st.write(f"**24h volume:** ${token['Volume 24h USD']:,.0f}")

with right:
    st.write(f"**Score:** {token['Score']}/100")
    st.write(f"**24h change:** {token['Change 24h %']:.2f}%")
    st.write(f"**Buys:** {token['Buys']}")
    st.write(f"**Sells:** {token['Sells']}")
    st.write(f"**Pair age:** {token['Age Hours']} hours")

st.code(token["Address"], language=None)

if token["Pair URL"]:
    st.link_button(
        "Open on DEX Screener",
        token["Pair URL"],
    )

st.caption(
    "Last scan: "
    + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
)

st.caption(
    "This scanner samples recent token profiles. "
    "It does not detect every new launch or guarantee early entry."
)




