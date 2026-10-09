

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    st_autorefresh = None


# ============================================================
# MEME RADAR AI — EARLY DISCOVERY + ALERTS + RISK SCREENING
# ============================================================

st.set_page_config(
    page_title="Meme Radar AI",
    page_icon="🎯",
    layout="wide",
)

DEX = "https://api.dexscreener.com"
RUGCHECK = "https://api.rugcheck.xyz/v1"
TIMEOUT = 10
HEADERS = {"User-Agent": "MemeRadarAI/3.0"}

for key, default in {
    "results": [],
    "previous_results": {},
    "alert_history": [],
    "last_scan": None,
    "scan_number": 0,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


# ============================================================
# API HELPERS
# ============================================================

def get_json(url):
    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        if response.status_code != 200:
            return None
        return response.json()
    except (requests.RequestException, ValueError):
        return None


def num(value, default=0.0):
    try:
        return float(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def money(value):
    value = num(value)
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.2f}K"
    return f"${value:,.2f}"


def pct(value):
    return "Unknown" if value is None else f"{num(value):.2f}%"


def age_text(hours):
    if hours is None:
        return "Unknown"
    if hours < 1:
        return f"{max(1, int(hours * 60))} min"
    if hours < 24:
        return f"{hours:.1f} hours"
    return f"{hours / 24:.1f} days"


# ============================================================
# EARLY TOKEN DISCOVERY
# ============================================================

def discover_tokens():
    """
    Discover Solana candidates from recent DexScreener feeds.
    These feeds are discovery sources, not a complete feed of
    every newly created Solana token.
    """
    endpoints = [
        ("Latest profiles", f"{DEX}/token-profiles/latest/v1"),
        ("Latest boosts", f"{DEX}/token-boosts/latest/v1"),
        ("Top boosts", f"{DEX}/token-boosts/top/v1"),
    ]

    found = {}

    for source, url in endpoints:
        data = get_json(url)

        if not isinstance(data, list):
            continue

        for item in data:
            if not isinstance(item, dict):
                continue
            if item.get("chainId") != "solana":
                continue

            address = item.get("tokenAddress")
            if not address:
                continue

            if address not in found:
                found[address] = {
                    "address": address,
                    "sources": [],
                    "boosted": False,
                }

            if source not in found[address]["sources"]:
                found[address]["sources"].append(source)

            if "boost" in source.lower():
                found[address]["boosted"] = True

    return list(found.values())


# ============================================================
# MARKET DATA
# ============================================================

def get_best_pair(address):
    data = get_json(
        f"{DEX}/token-pairs/v1/solana/{address}"
    )

    if not isinstance(data, list):
        return None

    pairs = [
        pair for pair in data
        if isinstance(pair, dict)
        and pair.get("chainId") == "solana"
    ]

    if not pairs:
        return None

    return max(
        pairs,
        key=lambda pair: num(
            (pair.get("liquidity") or {}).get("usd")
        ),
    )


def get_rugcheck(address):
    data = get_json(
        f"{RUGCHECK}/tokens/{address}/report"
    )
    return data if isinstance(data, dict) else None


# ============================================================
# HOLDER CONCENTRATION
# ============================================================

def analyze_holders(report):
    unknown = {
        "status": "UNKNOWN",
        "top1": None,
        "top5": None,
        "top10": None,
        "count": 0,
    }

    if not isinstance(report, dict):
        return unknown

    holders = report.get("topHolders")

    if not isinstance(holders, list):
        return unknown

    values = []

    for holder in holders:
        if not isinstance(holder, dict):
            continue

        try:
            value = float(holder["pct"])
        except (KeyError, TypeError, ValueError):
            continue

        if 0 <= value <= 100:
            values.append(value)

    if not values:
        return unknown

    values.sort(reverse=True)

    top1 = sum(values[:1])
    top5 = sum(values[:5])
    top10 = sum(values[:10])

    if top1 >= 20 or top5 >= 50 or top10 >= 70:
        status = "HIGH"
    elif top1 >= 10 or top5 >= 30 or top10 >= 50:
        status = "MODERATE"
    else:
        status = "LOWER"

    return {
        "status": status,
        "top1": round(top1, 2),
        "top5": round(top5, 2),
        "top10": round(top10, 2),
        "count": len(values),
    }


# ============================================================
# REPORTED LIQUIDITY LOCK
# ============================================================

def analyze_lp(report):
    unknown = {
        "status": "UNKNOWN",
        "minimum": None,
        "maximum": None,
        "markets": [],
    }

    if not isinstance(report, dict):
        return unknown

    markets = report.get("markets")

    if not isinstance(markets, list):
        return unknown

    details = []

    for market in markets:
        if not isinstance(market, dict):
            continue

        lp = market.get("lp") or {}

        if not isinstance(lp, dict):
            continue

        try:
            locked = float(lp["lpLockedPct"])
        except (KeyError, TypeError, ValueError):
            continue

        if not 0 <= locked <= 100:
            continue

        details.append({
            "Market": str(
                market.get("marketType")
                or market.get("pubkey")
                or "Unknown market"
            ),
            "Reported locked %": round(locked, 2),
        })

    if not details:
        return unknown

    values = [item["Reported locked %"] for item in details]
    lowest = min(values)
    highest = max(values)

    if lowest == 0:
        status = "NO LOCK REPORTED"
    elif lowest < 100:
        status = "PARTIALLY LOCKED"
    else:
        status = "100% REPORTED LOCKED"

    return {
        "status": status,
        "minimum": lowest,
        "maximum": highest,
        "markets": details,
    }


# ============================================================
# SECURITY REPORT
# ============================================================

def analyze_security(report):
    if not isinstance(report, dict):
        return "UNKNOWN", [], None, "Unknown", "Unknown"

    risks = report.get("risks", [])
    if not isinstance(risks, list):
        risks = []

    cleaned = []
    levels = []

    for risk in risks:
        if not isinstance(risk, dict):
            continue

        level = str(risk.get("level") or "Unknown")
        levels.append(level.lower())

        cleaned.append({
            "Risk": str(
                risk.get("name")
                or risk.get("description")
                or "Unspecified risk"
            ),
            "Level": level,
            "Details": str(risk.get("description") or ""),
        })

    if any(
        level in ("danger", "critical", "high")
        for level in levels
    ):
        rating = "HIGH RISK"
    elif any(
        level in ("warn", "warning", "medium")
        for level in levels
    ):
        rating = "CAUTION"
    elif cleaned:
        rating = "REVIEW"
    else:
        rating = "NO RISKS LISTED"

    score = report.get("score_normalised")
    if score is None:
        score = report.get("score")

    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None

    token = report.get("token") or {}
    if not isinstance(token, dict):
        token = {}

    mint = token.get("mintAuthority")
    freeze = token.get("freezeAuthority")

    return (
        rating,
        cleaned,
        score,
        "Unknown" if mint is None else str(mint),
        "Unknown" if freeze is None else str(freeze),
    )


# ============================================================
# TOKEN ANALYSIS + EARLY DISCOVERY SCORE
# ============================================================

def analyze_token(candidate):
    address = candidate["address"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        pair_future = pool.submit(get_best_pair, address)
        report_future = pool.submit(get_rugcheck, address)

        pair = pair_future.result()
        report = report_future.result()

    if not isinstance(pair, dict):
        pair = {}

    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}

    if not isinstance(base, dict):
        base = {}
    if not isinstance(quote, dict):
        quote = {}

    if str(base.get("address", "")).lower() == address.lower():
        token = base
    elif str(quote.get("address", "")).lower() == address.lower():
        token = quote
    else:
        token = base

    liquidity_data = pair.get("liquidity") or {}
    volume_data = pair.get("volume") or {}
    changes = pair.get("priceChange") or {}
    transactions = pair.get("txns") or {}

    if not isinstance(liquidity_data, dict):
        liquidity_data = {}
    if not isinstance(volume_data, dict):
        volume_data = {}
    if not isinstance(changes, dict):
        changes = {}
    if not isinstance(transactions, dict):
        transactions = {}

    h24 = transactions.get("h24") or {}
    if not isinstance(h24, dict):
        h24 = {}

    liquidity = num(liquidity_data.get("usd"))
    volume = num(volume_data.get("h24"))
    change1h = num(changes.get("h1"))
    change6h = num(changes.get("h6"))
    change24h = num(changes.get("h24"))
    buys = num(h24.get("buys"))
    sells = num(h24.get("sells"))
    tx_count = buys + sells

    price = num(pair.get("priceUsd"))
    market_cap = num(
        pair.get("marketCap"),
        num(pair.get("fdv")),
    )

    age_hours = None
    created = pair.get("pairCreatedAt")

    if created:
        try:
            age_hours = max(
                0,
                (time.time() * 1000 - float(created)) / 3_600_000,
            )
        except (TypeError, ValueError):
            pass

    # Momentum score: ranks activity; it is not a prediction.
    momentum = 0
    momentum += min(15, max(0, change1h) * 1.5)
    momentum += min(25, max(0, change6h) * 0.5)
    momentum += min(25, max(0, change24h) * 0.25)

    if volume >= 10_000:
        momentum += 10
    if volume >= 50_000:
        momentum += 10
    if tx_count >= 100:
        momentum += 10
    if buys > sells and tx_count > 0:
        momentum += 5

    momentum = round(min(100, momentum), 1)

    # Early-stage score rewards recent pairs and basic activity.
    early = 0

    if age_hours is not None:
        if age_hours <= 1:
            early += 35
        elif age_hours <= 6:
            early += 30
        elif age_hours <= 24:
            early += 25
        elif age_hours <= 72:
            early += 15
        elif age_hours <= 168:
            early += 5

    if liquidity >= 5_000:
        early += 10
    if liquidity >= 20_000:
        early += 10
    if volume >= 5_000:
        early += 10
    if tx_count >= 25:
        early += 10
    if buys > sells and tx_count >= 10:
        early += 5
    if candidate.get("boosted"):
        early += 5

    early = min(100, early)

    combined = round(
        0.45 * early + 0.55 * momentum,
        1,
    )

    # Safety warnings.
    market_flags = []

    if not pair:
        market_flags.append("No pair data")
    if liquidity < 5_000:
        market_flags.append("Very low liquidity")
    elif liquidity < 10_000:
        market_flags.append("Low liquidity")
    if volume < 1_000:
        market_flags.append("Very low 24h volume")
    if sells >= 10 and sells > buys * 2:
        market_flags.append("Sell-heavy activity")
    if change24h >= 100:
        market_flags.append("Extreme 24h price increase")
    if change24h <= -50:
        market_flags.append("Large 24h price decline")

    holders = analyze_holders(report)
    lp = analyze_lp(report)
    security, risks, rug_score, mint, freeze = analyze_security(report)

    return {
        "Name": str(token.get("name") or "Unknown"),
        "Symbol": str(token.get("symbol") or "?"),
        "Address": address,
        "Price": price,
        "Market Cap": market_cap,
        "Liquidity": liquidity,
        "Volume 24h": volume,
        "Buys": int(buys),
        "Sells": int(sells),
        "Change 1h %": round(change1h, 2),
        "Change 6h %": round(change6h, 2),
        "Change 24h %": round(change24h, 2),
        "Age hours": age_hours,
        "Age": age_text(age_hours),
        "Early Discovery Score": early,
        "Momentum Score": momentum,
        "Combined Score": combined,
        "Security": security,
        "RugCheck score": rug_score,
        "Mint authority": mint,
        "Freeze authority": freeze,
        "Security risks": risks,
        "Holder status": holders["status"],
        "Top 1 %": holders["top1"],
        "Top 5 %": holders["top5"],
        "Top 10 %": holders["top10"],
        "Holder accounts": holders["count"],
        "LP lock": lp["status"],
        "LP minimum %": lp["minimum"],
        "LP maximum %": lp["maximum"],
        "LP markets": lp["markets"],
        "Market warnings": market_flags,
        "Sources": ", ".join(candidate.get("sources", [])),
        "Pair URL": pair.get("url", ""),
        "RugCheck URL": f"https://rugcheck.xyz/tokens/{address}",
    }


def scan_candidates(candidates, limit):
    results = []
    candidates = candidates[:limit]

    if not candidates:
        return results

    progress = st.progress(0, text="Analyzing candidates...")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(analyze_token, candidate)
            for candidate in candidates
        ]

        for index, future in enumerate(as_completed(futures), start=1):
            try:
                results.append(future.result())
            except Exception:
                pass

            progress.progress(
                index / len(futures),
                text=f"Analyzed {index} of {len(futures)} candidates",
            )

    progress.empty()

    return sorted(
        results,
        key=lambda item: item["Combined Score"],
        reverse=True,
    )


# ============================================================
# ALERT ENGINE
# ============================================================

def build_alerts(results, previous, settings):
    alerts = []
    now = time.strftime("%H:%M:%S")

    def add(token, kind, severity, message):
        alerts.append({
            "Time": now,
            "Severity": severity,
            "Token": f"{token['Name']} ({token['Symbol']})",
            "Address": token["Address"],
            "Alert": kind,
            "Details": message,
        })

    for token in results:
        address = token["Address"]
        old = previous.get(address)

        # Early discovery: newly observed token with a recent pair.
        age = token["Age hours"]

        if (
            age is not None
            and age <= settings["new_pair_hours"]
            and token["Liquidity"] >= settings["min_liquidity"]
            and token["Volume 24h"] >= settings["min_volume"]
        ):
            add(
                token,
                "EARLY DISCOVERY",
                "OPPORTUNITY",
                f"Pair age {age_text(age)}; liquidity "
                f"{money(token['Liquidity'])}; volume "
                f"{money(token['Volume 24h'])}. Review manually.",
            )

        # Watch signal.
        if (
            token["Combined Score"] >= settings["min_score"]
            and token["Liquidity"] >= settings["min_liquidity"]
            and token["Volume 24h"] >= settings["min_volume"]
        ):
            add(
                token,
                "WATCH SIGNAL",
                "INFO",
                f"Combined score {token['Combined Score']:.1f}/100.",
            )

        if token["Change 1h %"] >= settings["surge_pct"]:
            add(
                token,
                "PRICE SURGE",
                "WARNING",
                f"1h price change +{token['Change 1h %']:.2f}%.",
            )

        if token["Change 1h %"] <= -settings["surge_pct"]:
            add(
                token,
                "PRICE DROP",
                "DANGER",
                f"1h price change {token['Change 1h %']:.2f}%.",
            )

        # Compare with the previous scan for liquidity deterioration.
        if old:
            old_liquidity = num(old.get("Liquidity"))
            new_liquidity = token["Liquidity"]

            if old_liquidity > 0:
                drop = (
                    (old_liquidity - new_liquidity)
                    / old_liquidity
                ) * 100

                if drop >= settings["liquidity_drop_pct"]:
                    add(
                        token,
                        "LIQUIDITY DROP",
                        "DANGER",
                        f"Liquidity fell {drop:.1f}% since the last scan.",
                    )

            if (
                token["Change 1h %"] < old.get("Change 1h %", 0)
                and token["Sells"] > token["Buys"] * 2
                and token["Sells"] >= 10
            ):
                add(
                    token,
                    "SELL PRESSURE",
                    "WARNING",
                    "Price momentum weakened while reported sells outnumber buys.",
                )

        if token["Security"] == "HIGH RISK":
            add(
                token,
                "SECURITY RISK",
                "DANGER",
                "RugCheck reports high-severity risks.",
            )

        if token["Holder status"] == "HIGH":
            add(
                token,
                "HOLDER CONCENTRATION",
                "WARNING",
                f"Top 1: {pct(token['Top 1 %'])}; "
                f"top 5: {pct(token['Top 5 %'])}.",
            )

        if token["LP lock"] == "NO LOCK REPORTED":
            add(
                token,
                "LIQUIDITY LOCK WARNING",
                "DANGER",
                "At least one market with lock data reports 0% locked.",
            )

    # Deduplicate identical alerts within the current scan.
    unique = {}
    for alert in alerts:
        key = (
            alert["Address"],
            alert["Alert"],
            alert["Details"],
        )
        unique[key] = alert

    return list(unique.values())


# ============================================================
# DASHBOARD + CONTROLS
# ============================================================

st.title("🎯 Meme Radar AI")
st.caption(
    "Early discovery · Momentum · Holder concentration · "
    "Liquidity-lock reports · Market alerts"
)

with st.sidebar:
    st.header("Scanner settings")

    scan_limit = st.slider(
        "Candidates to analyze",
        5, 30, 15,
    )

    min_liquidity = st.number_input(
        "Minimum liquidity ($)",
        min_value=0,
        value=10_000,
        step=1_000,
    )

    min_volume = st.number_input(
        "Minimum 24h volume ($)",
        min_value=0,
        value=5_000,
        step=1_000,
    )

    min_score = st.slider(
        "Minimum combined score",
        0, 100, 55,
    )

    new_pair_hours = st.slider(
        "Early discovery: pair age up to (hours)",
        1, 72, 24,
    )

    surge_pct = st.slider(
        "Price move alert (%)",
        5, 100, 15,
    )

    liquidity_drop_pct = st.slider(
        "Liquidity drop alert (%)",
        10, 80, 30,
    )

    auto_refresh = st.checkbox(
        "Auto-refresh dashboard",
        value=False,
    )

    refresh_seconds = st.selectbox(
        "Refresh interval",
        [60, 120, 300],
        index=1,
        format_func=lambda x: f"{x} seconds",
    )

    scan_button = st.button(
        "🔎 Scan now",
        type="primary",
        use_container_width=True,
    )

    st.caption(
        "Automatic refreshing is not the same as guaranteed "
        "background monitoring. Keep the app open."
    )

settings = {
    "min_liquidity": min_liquidity,
    "min_volume": min_volume,
    "min_score": min_score,
    "new_pair_hours": new_pair_hours,
    "surge_pct": surge_pct,
    "liquidity_drop_pct": liquidity_drop_pct,
}

if auto_refresh and st_autorefresh is not None:
    st_autorefresh(
        interval=refresh_seconds * 1000,
        key="meme_radar_refresh",
    )

should_scan = scan_button

if auto_refresh:
    last_tick = st.session_state.get("last_tick")
    if (
        last_tick is None
        or time.time() - last_tick >= refresh_seconds
    ):
        should_scan = True

if should_scan:
    st.session_state["last_tick"] = time.time()

    with st.spinner("Discovering recent Solana tokens..."):
        candidates = discover_tokens()

    if not candidates:
        st.warning(
            "No candidates returned. The discovery provider may be "
            "temporarily unavailable. Try scanning again."
        )
    else:
        previous = {
            item["Address"]: item
            for item in st.session_state["results"]
        }

        results = scan_candidates(candidates, scan_limit)

        if results:
            new_alerts = build_alerts(results, previous, settings)

            # Keep recent alert history for this running session.
            history = st.session_state["alert_history"]
            history = new_alerts + history
            st.session_state["alert_history"] = history[:200]

            st.session_state["previous_results"] = previous
            st.session_state["results"] = results
            st.session_state["last_scan"] = time.strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            st.session_state["scan_number"] += 1

results = st.session_state["results"]

if not results:
    st.info(
        "Click **Scan now** to discover and analyze Solana tokens."
    )
    st.stop()

st.caption(
    f"Last scan: {st.session_state['last_scan'] or 'Unknown'}"
)

df = pd.DataFrame(results)

# ============================================================
# OVERVIEW
# ============================================================

a, b, c, d = st.columns(4)

a.metric("Tokens analyzed", len(df))
b.metric(
    "Recent pairs",
    int(
        df["Age hours"].apply(
            lambda value: value is not None
            and pd.notna(value)
            and value <= new_pair_hours
        ).sum()
    ),
)
c.metric(
    "High-risk reports",
    int((df["Security"] == "HIGH RISK").sum()),
)
d.metric(
    "High-score candidates",
    int((df["Combined Score"] >= min_score).sum()),
)

# ============================================================
# ALERTS
# ============================================================

st.subheader("🚨 Alerts")

alerts = st.session_state["alert_history"]

if alerts:
    alert_df = pd.DataFrame(alerts)

    severity_filter = st.multiselect(
        "Show alert severity",
        ["DANGER", "WARNING", "OPPORTUNITY", "INFO"],
        default=["DANGER", "WARNING", "OPPORTUNITY", "INFO"],
    )

    visible_alerts = alert_df[
        alert_df["Severity"].isin(severity_filter)
    ]

    st.dataframe(
        visible_alerts,
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info(
        "No alerts yet. Scan again after more market data becomes available."
    )

if st.button("Clear alert history"):
    st.session_state["alert_history"] = []
    st.rerun()

# ============================================================
# EARLY DISCOVERY WATCHLIST
# ============================================================

st.subheader("🆕 Early discovery watchlist")

early_df = df[
    df["Age hours"].notna()
    & (df["Age hours"] <= new_pair_hours)
].copy()

if early_df.empty:
    st.info(
        "No pairs within the selected age window were found "
        "in the candidates scanned."
    )
else:
    early_df = early_df.sort_values(
        ["Early Discovery Score", "Liquidity"],
        ascending=False,
    )

    st.dataframe(
        early_df[
            [
                "Name",
                "Symbol",
                "Age",
                "Price",
                "Liquidity",
                "Volume 24h",
                "Buys",
                "Sells",
                "Change 1h %",
                "Early Discovery Score",
                "Combined Score",
                "Security",
                "Holder status",
                "LP lock",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )

# ============================================================
# MAIN RANKED WATCHLIST
# ============================================================

st.subheader("📊 Ranked watchlist")

filtered = df[
    (df["Liquidity"] >= min_liquidity)
    & (df["Volume 24h"] >= min_volume)
    & (df["Combined Score"] >= min_score)
].copy()

if filtered.empty:
    st.info("No tokens currently meet all selected filters.")
else:
    filtered = filtered.sort_values(
        "Combined Score",
        ascending=False,
    )

    st.dataframe(
        filtered[
            [
                "Name",
                "Symbol",
                "Age",
                "Price",
                "Market Cap",
                "Liquidity",
                "Volume 24h",
                "Change 1h %",
                "Change 24h %",
                "Early Discovery Score",
                "Momentum Score",
                "Combined Score",
                "Security",
                "Holder status",
                "LP lock",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )

# ============================================================
# TOKEN INSPECTOR
# ============================================================

st.subheader("🔬 Token inspector")

selected = st.selectbox(
    "Choose a token",
    df["Address"].tolist(),
    format_func=lambda address: (
        f"{df.loc[df['Address'] == address, 'Symbol'].iloc[0]} — "
        f"{df.loc[df['Address'] == address, 'Name'].iloc[0]}"
    ),
)

matches = df[df["Address"] == selected]

if not matches.empty:
    row = matches.iloc[0]

    st.markdown(f"### {row['Name']} ({row['Symbol']})")

    x1, x2, x3, x4 = st.columns(4)
    x1.metric("Early score", f"{row['Early Discovery Score']}/100")
    x2.metric("Momentum", f"{row['Momentum Score']}/100")
    x3.metric("Liquidity", money(row["Liquidity"]))
    x4.metric("24h volume", money(row["Volume 24h"]))

    st.write(f"**Security:** {row['Security']}")
    st.write(f"**Holder concentration:** {row['Holder status']}")
    st.write(f"**Reported LP lock:** {row['LP lock']}")

    if row["Pair URL"]:
        st.markdown(f"[Open DexScreener]({row['Pair URL']})")

    st.markdown(f"[Open RugCheck]({row['RugCheck URL']})")

    with st.expander("Holder concentration"):
        st.write(f"Top reported account: {pct(row['Top 1 %'])}")
        st.write(f"Top 5 reported accounts: {pct(row['Top 5 %'])}")
        st.write(f"Top 10 reported accounts: {pct(row['Top 10 %'])}")
        st.write(f"Accounts counted: {row['Holder accounts']}")

        st.caption(
            "These are provider-reported accounts, not necessarily unique "
            "beneficial owners. Pool and burn accounts may affect totals."
        )

    with st.expander("Liquidity-lock report"):
        st.write(f"Status: {row['LP lock']}")
        st.write(f"Lowest reported lock: {pct(row['LP minimum %'])}")
        st.write(f"Highest reported lock: {pct(row['LP maximum %'])}")

        markets = row["LP markets"]
        if isinstance(markets, list) and markets:
            st.dataframe(
                pd.DataFrame(markets),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No usable market-level lock data was returned.")

        st.warning(
            "Reported lock percentages are not independent verification. "
            "Check the pool, LP token ownership, lock expiry and transaction "
            "before relying on the result."
        )

    with st.expander("Security report"):
        st.write(f"RugCheck score: {row['RugCheck score']}")
        st.write(f"Mint authority: {row['Mint authority']}")
        st.write(f"Freeze authority: {row['Freeze authority']}")

        risks = row["Security risks"]
        if isinstance(risks, list) and risks:
            st.dataframe(
                pd.DataFrame(risks),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info(
                "No risk entries were returned, or the report was unavailable. "
                "That does not prove the token is safe."
            )

        flags = row["Market warnings"]
        if flags:
            st.write("Market warnings:")
            for flag in flags:
                st.write(f"- {flag}")

    with st.expander("Token address"):
        st.code(row["Address"], language=None)

st.divider()

st.caption(
    "Meme Radar AI is a screening dashboard, not a trading bot. It does not "
    "execute trades. Early-discovery feeds are incomplete, prices can change "
    "rapidly, and security or liquidity reports can be missing or misleading. "
    "Never risk money based only on a score or alert."
)
