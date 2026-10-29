import json
import random
import threading
import time
from pathlib import Path

import pandas as pd
import pydeck as pdk
import requests
import streamlit as st

st.set_page_config(page_title="Pub Crawl 2026", page_icon="🍻", layout="wide")

# ---------------------------------------------------------------------------
# 1. THE PUBS (in crawl order). "query" is what we search to find the location.
#    If one lands in the wrong place, fix it in the Admin tab (no code needed).
# ---------------------------------------------------------------------------
PUBS = [
    {"name": "Laurieston Bar",           "query": "Laurieston Bar, Bridge Street, Glasgow"},
    {"name": "MacSorley's",              "query": "MacSorley's, Jamaica Street, Glasgow"},
    {"name": "Sloans",                   "query": "Sloans, Argyll Arcade, Glasgow"},
    {"name": "Cairns Bar",               "query": "Cairns Bar, Glasgow"},
    {"name": "Poko Loko",                "query": "Poko Loko, Glasgow"},
    {"name": "O'Neill's Merchant Square","query": "O'Neill's, Merchant Square, Glasgow"},
    {"name": "Tap Yard Merchant Square", "query": "Tap Yard, Merchant Square, Glasgow"},
    {"name": "Blane Valley",             "query": "Blane Valley, Glasgow"},
    {"name": "Waxy O'Connor's",          "query": "Waxy O'Connor's, West George Street, Glasgow"},
    {"name": "Stereo",                   "query": "Stereo, Renfield Lane, Glasgow"},
    {"name": "Kitty",                  "query": "Kittie's, Glasgow"},
]
PUB_NAMES = [p["name"] for p in PUBS]

try:
    ADMIN_PASSWORD = st.secrets.get("admin_password", "crawl")
except Exception:
    ADMIN_PASSWORD = "crawl"

STATE_FILE = Path("crawl_state.json")
LOC_FILE = Path("locations.json")  # {"auto": {name: [lat, lon]}, "manual": {name: [lat, lon]}}
HEADERS = {"User-Agent": "pub-crawl-2026-streamlit/1.0"}


# ---------------------------------------------------------------------------
# 2. Shared state: rules + crawl progress (everyone sees the same thing)
# ---------------------------------------------------------------------------
@st.cache_resource
def get_lock():
    return threading.Lock()


def load_state():
    state = {"rules": {}, "progress": 0}
    if STATE_FILE.exists():
        state.update(json.loads(STATE_FILE.read_text()))
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def submit_rule(rule, author):
    """Instantly match the rule to a random pub that doesn't have one yet."""
    with get_lock():
        state = load_state()
        free = [n for n in PUB_NAMES if n not in state["rules"]]
        if not free:
            return None
        pub = random.choice(free)
        state["rules"][pub] = {"rule": rule.strip(), "by": author.strip() or "Anonymous"}
        save_state(state)
        return pub


def set_progress(value):
    with get_lock():
        state = load_state()
        state["progress"] = max(0, min(len(PUBS), value))
        save_state(state)


def reset_all():
    with get_lock():
        save_state({"rules": {}, "progress": 0})


# ---------------------------------------------------------------------------
# 3. Locations (auto-geocoded once, can be overridden manually)
# ---------------------------------------------------------------------------
def load_locs():
    if LOC_FILE.exists():
        return json.loads(LOC_FILE.read_text())
    return {"auto": {}, "manual": {}}


def save_locs(locs):
    LOC_FILE.write_text(json.dumps(locs, indent=2))


def geocode(query):
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "gb",
                    "viewbox": "-4.40,55.92,-4.10,55.80", "bounded": 1},
            headers=HEADERS, timeout=10,
        )
        data = r.json()
        if data:
            return [float(data[0]["lat"]), float(data[0]["lon"])]
    except Exception:
        pass
    return None


def ensure_geocoded(force=False):
    locs = load_locs()
    changed = False
    for p in PUBS:
        if force or (p["name"] not in locs["auto"] and p["name"] not in locs["manual"]):
            res = geocode(p["query"])
            if res:
                locs["auto"][p["name"]] = res
                changed = True
            time.sleep(1.1)  # be polite to the free geocoder (max 1 request/second)
    if changed:
        save_locs(locs)
    return locs


def get_positions():
    locs = load_locs()
    out = {}
    for n in PUB_NAMES:
        pos = locs["manual"].get(n) or locs["auto"].get(n)
        if pos:
            out[n] = pos
    return out


# ---------------------------------------------------------------------------
# 4. Walking directions (leg by leg)
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=3600)
def get_leg(a, b):
    """a, b = (lat, lon). Returns dict(coords, steps, distance, duration)."""
    url = (f"https://routing.openstreetmap.de/routed-foot/route/v1/foot/"
           f"{a[1]},{a[0]};{b[1]},{b[0]}?overview=full&geometries=geojson&steps=true")
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        route = r.json()["routes"][0]
        steps = [s for s in route["legs"][0]["steps"]]
        return {"coords": route["geometry"]["coordinates"], "steps": steps,
                "distance": route["distance"], "duration": route["duration"]}
    except Exception:
        return {"coords": [[a[1], a[0]], [b[1], b[0]]], "steps": [], "distance": None, "duration": None}


def step_text(s):
    m = s["maneuver"]
    t, mod = m.get("type", ""), m.get("modifier", "")
    name = s.get("name") or "the path"
    if t == "depart":
        return f"Start on {name}"
    if t == "arrive":
        return "You have arrived 🍻"
    if t in ("turn", "end of road", "fork", "off ramp", "on ramp"):
        return f"Turn {mod} onto {name}" if mod != "straight" else f"Continue onto {name}"
    if t == "roundabout":
        return f"At the roundabout, continue onto {name}"
    return f"Continue {mod} on {name}".replace("  ", " ")


# ---------------------------------------------------------------------------
# 5. Map
# ---------------------------------------------------------------------------
def draw_map(positions, progress):
    ordered = [(i, n) for i, n in enumerate(PUB_NAMES) if n in positions]
    rows = []
    for i, n in ordered:
        if i < progress:
            colour = [46, 160, 67, 230]    # visited = green
        elif i == progress:
            colour = [245, 140, 20, 240]   # next stop = orange
        else:
            colour = [150, 150, 150, 220]  # later = grey
        rows.append({"name": n, "order": str(i + 1), "lat": positions[n][0],
                     "lon": positions[n][1], "color": colour})
    df = pd.DataFrame(rows)

    faint, current = [], []
    for k in range(len(ordered) - 1):
        (i, a), (_, b) = ordered[k], ordered[k + 1]
        leg = get_leg(tuple(positions[a]), tuple(positions[b]))
        (current if i + 1 == progress else faint).append({"path": leg["coords"]})
    # leg towards the next pub (from the previous one)
    layers = [
        pdk.Layer("PathLayer", data=faint, get_path="path", get_color=[160, 170, 190, 160],
                  get_width=4, width_min_pixels=3),
        pdk.Layer("PathLayer", data=current, get_path="path", get_color=[30, 100, 220, 255],
                  get_width=8, width_min_pixels=6),
        pdk.Layer("ScatterplotLayer", data=df, get_position="[lon, lat]", get_fill_color="color",
                  get_radius=30, radius_min_pixels=13, pickable=True),
        pdk.Layer("TextLayer", data=df, get_position="[lon, lat]", get_text="order",
                  get_size=16, get_color=[255, 255, 255, 255]),
    ]
    focus = df[df["order"] == str(min(progress, len(PUBS) - 1) + 1)]
    centre = focus.iloc[0] if len(focus) else df.iloc[0]
    view = pdk.ViewState(latitude=float(df["lat"].mean()), longitude=float(df["lon"].mean()), zoom=14)
    st.pydeck_chart(pdk.Deck(
        layers=layers, initial_view_state=view,
        map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
        tooltip={"text": "{order}. {name}"},
    ), height=520)


# ---------------------------------------------------------------------------
# 6. UI
# ---------------------------------------------------------------------------
st.title("🍻 Pub Crawl 2026")

if "geocoded" not in st.session_state:
    with st.spinner("Finding the pubs on the map"):
        ensure_geocoded()
    st.session_state["geocoded"] = True

positions = get_positions()
state = load_state()
progress = state["progress"]
rules = state["rules"]
missing = [n for n in PUB_NAMES if n not in positions]
if missing:
    st.warning("Couldn't locate: " + ", ".join(missing) + ". Set them in the Admin tab.")

tab_crawl, tab_submit, tab_rules, tab_admin = st.tabs(
    ["Crawl", "Submit a rule", "Rules so far", "Admin"]
)

# ---- Crawl tab: map + directions to the next pub + reveal rule on arrival ----
with tab_crawl:
    if st.button("Refresh"):
        st.rerun()

    if positions:
        draw_map(positions, progress)
        st.caption("🟢 done · 🟠 next stop · ⚪ later · blue line = your current walk")

    if progress >= len(PUBS):
        st.success("That's every pub. The crawl is complete!")
    else:
        target = PUB_NAMES[progress]
        st.subheader(f"Next stop {progress + 1}/{len(PUBS)}: {target}")

        if target in positions:
            dest = positions[target]
            if progress == 0:
                st.write("Head to the first pub")
                origin_param = ""
            else:
                prev = PUB_NAMES[progress - 1]
                origin_param = f"&origin={positions[prev][0]},{positions[prev][1]}" if prev in positions else ""
                if prev in positions:
                    leg = get_leg(tuple(positions[prev]), tuple(dest))
                    if leg["distance"]:
                        st.write(f"🚶 From **{prev}**: about {leg['distance']:.0f} m, "
                                 f"~{max(1, round(leg['duration'] / 60))} min walk")
                    with st.expander("Turn-by-turn directions", expanded=True):
                        for s in leg["steps"]:
                            d = f" ({s['distance']:.0f} m)" if s["distance"] else ""
                            st.markdown(f"- {step_text(s)}{d}")
            st.link_button(
                " Open walking directions in Google Maps",
                f"https://www.google.com/maps/dir/?api=1{origin_param}"
                f"&destination={dest[0]},{dest[1]}&travelmode=walking",
            )

        if st.button(f" We've arrived at {target} – reveal the rule", type="primary"):
            set_progress(progress + 1)
            st.rerun()

    # rule for the pub we last arrived at
    if progress > 0:
        here = PUB_NAMES[progress - 1]
        st.divider()
        st.markdown(f"### Rule at {here}")
        entry = rules.get(here)
        if entry:
            st.info(f"**{entry['rule']}**\n\n— {entry['by']}")
        else:
            st.write("Nobody wrote a rule for this pub. Drink freely!")

# ---- Submit tab ----
with tab_submit:
    remaining = len(PUBS) - len(rules)
    st.subheader("Add a rule")
    st.metric("Pubs still without a rule", remaining)
    if remaining == 0:
        st.success("Every pub has a rule. Nothing left to submit!")
    else:
        with st.form("rule_form", clear_on_submit=True):
            author = st.text_input("Your name")
            rule = st.text_area("Your rule")
            sent = st.form_submit_button("Submit rule")
        if sent:
            if not rule.strip():
                st.warning("Write a rule first.")
            else:
                pub = submit_rule(rule, author)
                if pub:
                    st.success("Rule locked in and matched to a pub!")
                else:
                    st.error("Too late, every pub already has a rule.")

# ---- Rules so far (only pubs already reached) ----
with tab_rules:
    st.subheader("Rules from the pubs you've reached")
    if progress == 0:
        st.write("Nothing revealed yet. Get to the first pub!")
    for i in range(progress):
        n = PUB_NAMES[i]
        entry = rules.get(n)
        with st.container(border=True):
            st.markdown(f"**{i + 1}. {n}**")
            st.write(entry["rule"] if entry else "No rule for this pub.")

# ---- Admin ----
with tab_admin:
    pw = st.text_input("Password", type="password")
    if pw == ADMIN_PASSWORD:
        st.markdown("#### Crawl progress")
        c1, c2 = st.columns(2)
        if c1.button("Go back one pub"):
            set_progress(progress - 1)
            st.rerun()
        if c2.button("Skip forward one pub"):
            set_progress(progress + 1)
            st.rerun()

        st.markdown("All assignments")
        st.dataframe(pd.DataFrame([
            {"#": i + 1, "Pub": n, "Rule": rules.get(n, {}).get("rule", "—"),
             "By": rules.get(n, {}).get("by", "—")} for i, n in enumerate(PUB_NAMES)
        ]), hide_index=True)

        st.markdown("#### Fix pub locations")
        st.caption("Right-click a pub on Google Maps and click the coordinates to copy them.")
        locs = load_locs()
        edit_df = pd.DataFrame([
            {"Pub": n,
             "lat": (positions.get(n) or [None, None])[0],
             "lon": (positions.get(n) or [None, None])[1]} for n in PUB_NAMES
        ])
        edited = st.data_editor(edit_df, hide_index=True, disabled=["Pub"], key="loc_editor")
        if st.button("Save locations"):
            for _, row in edited.iterrows():
                if pd.notna(row["lat"]) and pd.notna(row["lon"]):
                    locs["manual"][row["Pub"]] = [float(row["lat"]), float(row["lon"])]
            save_locs(locs)
            get_leg.clear()
            st.rerun()

        st.markdown("#### Danger zone")
        if st.button("Reset all rules and progress"):
            reset_all()
            st.rerun()
    elif pw:
        st.error("Wrong password.")
