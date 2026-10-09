
import time
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st

st.set_page_config(
    page_title="Meme Radar AI",
    page_icon="🛰️",
    layout="wide",
)

API = "https://api.dexscreener.com"
HEADERS = {"User-Agent": "MemeRadarAI/1.0"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


@st.cache_data(ttl=60, show_spinner=False)
def get_profiles():
    url = f"{API}/token-profiles/latest/v1"
    response = SESSION.get(url, timeout=15)
    response.raise_for_status()
    data = response.json()

    return [
        item for item in data
        if item.get("chainId") == "solana"
        and item.get("tokenAddress")
    ][:40]


@st.cache_data(ttl=60, show_spinner=False)
def get_pairs(address):
    url = f"{API}/token-pairs/v1/solana/{address}"
    response = SESSION.get(url, timeout=15)
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def number(value, default=0.0):
    try:
        result = float(value)
        if pd.notna(result):
            return result
    except (TypeError, ValueError):
        pass
    return default


def score_token(liquidity, volume, change, buys, sells, age_hours):
    # Heuristic ranking, not a prediction of future returns.
    score = 0

    if liquidity >= 5000:
        score += 15
    if liquidity >= 20000:
        score += 10

    if volume >= 5000:
        score += 10
    if volume >= 25000:
        score += 10

    if buys + sells > 0:
        buy_ratio = buys / (buys + sells)
        if buy_ratio >= 0.55:
            score += 15
        if buy_ratio >= 0.65:
            score += 10

    if 0 < age_hours <= 24:
        score += 10

    if 0 < change <= 20:
        score += 10
    elif 20 < change <= 60:
        score += 15
    elif change > 60:
        score += 5  # Large spikes often carry elevated risk.

    if liquidity < 2000:
        score -= 25
    if volume == 0:
        score -= 10
    if change < -20:
        score -= 10

    return max(0, min(100, score))


def build_rows():
    profiles = get_profiles()
    rows = []
    errors = 0

    progress = st.progress(0, text="Checking recent Solana token profiles...")

    for index, profile in enumerate(profiles):
        address = profile["tokenAddress"]

        try:
            pairs = get_pairs(address)
            # Prefer pairs with the greatest available liquidity.
            pairs.sort(
                key=lambda pair: number(
                    (pair.get("liquidity") or {}).get("usd")
                ),
                reverse=True,
            )

            if not pairs:
                continue

            pair = pairs[0]
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
            txns = (pair.get("txns") or {}).get("h24") or {}
            buys = int(number(txns.get("buys")))
            sells = int(number(txns.get("sells")))

            created = pair.get("pairCreatedAt")
            age_hours = None

            if created:
                age_hours = max(
                    0,
                    (time.time() * 1000 - number(created)) / 3600000,
                )

            # Do not invent an age when pair creation time is missing.
            score = score_token(
                liquidity, volume, change, buys, sells,
                age_hours if age_hours is not None else -1,
            )

            rows.append({
                "Token": base.get("name") or "Unknown",
                "Symbol": base.get("symbol") or "?",
                "Price USD": number(pair.get("priceUsd")),
                "Market Cap": number(
                    pair.get("marketCap") or pair.get("fdv")
                ),
                "Liquidity USD": liquidity,
                "24h Volume": volume,
                "24h Change %": change,
                "Buys": buys,
                "Sells": sells,
                "Age Hours": round(age_hours, 1)
                if age_hours is not None else None,
                "Score": score,
                "Pair URL": pair.get("url", ""),
                "Address": address,
            })

        except (requests.RequestException, ValueError, TypeError):
            errors += 1

        progress.progress(
            (index + 1) / max(len(profiles), 1),
            text=f"Scanning token {index + 1} of {len(profiles)}...",
        )

    progress.empty()
    return rows, errors


st.title("🛰️ Meme Radar AI")
st.caption("Solana discovery scanner | Live public market data")

st.warning(
    "Research tool only. Scores are experimental heuristics, "
    "not AI predictions or financial advice. Token data may be "
    "incomplete, manipulated, or delayed."
)

with st.sidebar:
    st.header("Scanner settings")
    min_liquidity = st.number_input(
        "Minimum liquidity ($)", min_value=0, value=5000, step=1000
    )
    min_volume = st.number_input(
        "Minimum 24h volume ($)", min_value=0, value=5000, step=1000
    )
    max_age = st.number_input(
        "Maximum pair age (hours)", min_value=1, value=72, step=12
    )
    refresh = st.button("Refresh market data", use_container_width=True)

if refresh:
    get_profiles.clear()
    get_pairs.clear()

if "rows" not in st.session_state or refresh:
    try:
        with st.spinner("Fetching token market data..."):
            st.session_state.rows, st.session_state.errors = build_rows()
    except requests.RequestException as exc:
        st.error(f"Market data request failed: {exc}")
        st.stop()

rows = st.session_state.rows
errors = st.session_state.errors

if not rows:
    st.error(
        "No pairs returned. Try refreshing later or check the API connection."
    )
    st.stop()

df = pd.DataFrame(rows)
filtered = df[
    (df["Liquidity USD"] >= min_liquidity)
    & (df["24h Volume"] >= min_volume)
    & (
        df["Age Hours"].isna()
        | (df["Age Hours"] <= max_age)
    )
].copy()

filtered = filtered.sort_values(
    ["Score", "24h Volume"], ascending=False
)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Pairs scanned", len(df))
c2.metric("Passed filters", len(filtered))
c3.metric(
    "Highest score",
    int(filtered["Score"].max()) if not filtered.empty else "None",
)
c4.metric("Data errors", errors)

st.subheader("Early momentum watchlist")
st.caption(
    "Profiles are sampled from the latest token-profile feed. "
    "This is not a complete feed of every new Solana token."
)

if filtered.empty:
    st.info("No tokens match your current filters. Lower the thresholds.")
else:
    display = filtered.rename(columns={
        "Price USD": "Price ($)",
        "Market Cap": "Market cap ($)",
        "Liquidity USD": "Liquidity ($)",
        "24h Volume": "Volume 24h ($)",
        "24h Change %": "Change 24h (%)",
        "Age Hours": "Pair age (hours)",
    })

    st.dataframe(
        display[
            [
                "Token", "Symbol", "Score", "Price ($)",
                "Market cap ($)", "Liquidity ($)",
                "Volume 24h ($)", "Change 24h (%)",
                "Pair age (hours)", "Buys", "Sells",
                "Pair URL", "Address",
            ]
        ],
        use_container_width=True,
        hide_index=True,
        column_config={
            "Pair URL": st.column_config.LinkColumn("DEX Screener"),
            "Price ($)": st.column_config.NumberColumn(format="%.10f"),
            "Market cap ($)": st.column_config.NumberColumn(format="$%.0f"),
            "Liquidity ($)": st.column_config.NumberColumn(format="$%.0f"),
            "Volume 24h ($)": st.column_config.NumberColumn(format="$%.0f"),
            "Change 24h (%)": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )

    st.download_button(
        "Download watchlist CSV",
        data=filtered.to_csv(index=False).encode("utf-8"),
        file_name="meme_radar_watchlist.csv",
        mime="text/csv",
    )

st.subheader("Inspect a token")
choice = st.selectbox(
    "Select a scanned token",
    options=df.index,
    format_func=lambda idx: (
        f'{df.loc[idx, "Symbol"]} | '
        f'{df.loc[idx, "Token"]} | '
        f'Score {df.loc[idx, "Score"]}'
    ),
)

token = df.loc[choice]
st.write(f"**Token address:** `{token['Address']}`")
st.write(f"**Pair age:** {token['Age Hours']} hours")
st.write(f"**Buy transactions:** {token['Buys']}")
st.write(f"**Sell transactions:** {token['Sells']}")
st.write(f"**Liquidity:** ${token['Liquidity USD']:,.0f}")
st.write(f"**24h volume:** ${token['24h Volume']:,.0f}")

if token["Pair URL"]:
    st.link_button("Open token on DEX Screener", token["Pair URL"])

st.caption(
    "Last refresh: " +
    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
)
