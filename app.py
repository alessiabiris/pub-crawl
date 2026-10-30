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
# 1. THE PUBS (in crawl order). "queries" are the searches used to find each pub.
#    If one lands in the wrong place, fix it in the Admin tab (no code needed).
#    "old_names" = earlier spellings, so saved locations still work after a rename.
# ---------------------------------------------------------------------------
PUBS = [
    {"name": "Laurieston Bar",           "queries": ["Laurieston Bar, 58 Bridge Street, Glasgow", "Laurieston Bar, Glasgow"]},
    {"name": "MacSorley's",              "queries": ["MacSorley's, Jamaica Street, Glasgow", "MacSorleys, Glasgow"]},
    {"name": "Sloans",                   "queries": ["Sloans, 108 Argyle Street, Glasgow", "Sloans, Argyll Arcade, Glasgow", "G2 8BH"]},
    {"name": "Cairns Bar",               "queries": ["Cairns Bar, 15 Miller Street, Glasgow", "Cairns Bar, Glasgow", "G1 1EA"]},
    {"name": "Poko Loko",                "queries": ["Poko Loko, Glasgow", "Poko Loko Tequila and Tacos, Glasgow", "G1 1SH"]},
    {"name": "O'Neill's Merchant Square","queries": ["O'Neill's, 71 Albion Street, Glasgow", "O'Neill's, Merchant Square, Glasgow", "G1 1NY"]},
    {"name": "Tap Yard Merchant Square", "queries": ["The Tap Yard, Bell Street, Glasgow", "The Tap Yard, Glasgow", "G1 1NU"]},
    {"name": "Blane Valley",             "queries": ["Blane Valley, Glasgow", "Blane Valley Bar, Glasgow"]},
    {"name": "Waxy O'Connor's",          "queries": ["Waxy O'Connor's, West George Street, Glasgow", "Waxy O'Connors, Glasgow"]},
    {"name": "Stereo",                   "queries": ["Stereo, Renfield Lane, Glasgow", "Stereo, Glasgow"]},
    {"name": "Kitty",                    "queries": ["Kittie's, Glasgow", "Kitty's, Glasgow"], "old_names": ["Kitties"]},
]
PUB_NAMES = [p["name"] for p in PUBS]

# The admin password comes from Streamlit's Secrets (never written in the code).
try:
    ADMIN_PASSWORD = st.secrets["admin_password"]
except Exception:
    ADMIN_PASSWORD = None

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
    state = {"rules": {}, "progress": 0, "skipped": [], "started": False}
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
        value = max(0, min(len(PUBS), value))
        state["progress"] = value
        # going back un-skips any pub that is ahead of the new position
        state["skipped"] = [n for n in state["skipped"] if PUB_NAMES.index(n) < value]
        save_state(state)


def skip_pub():
    """Skip the next pub: move on without revealing its rule."""
    with get_lock():
        state = load_state()
        if state["progress"] < len(PUBS):
            name = PUB_NAMES[state["progress"]]
            if name not in state["skipped"]:
                state["skipped"].append(name)
            state["progress"] += 1
            save_state(state)


def set_started(value):
    with get_lock():
        state = load_state()
        state["started"] = value
        save_state(state)


def reset_all():
    with get_lock():
        save_state({"rules": {}, "progress": 0, "skipped": [], "started": False})


# ---------------------------------------------------------------------------
# 3. Locations (auto-found once, can be overridden manually)
# ---------------------------------------------------------------------------
def load_locs():
    if LOC_FILE.exists():
        return json.loads(LOC_FILE.read_text())
    return {"auto": {}, "manual": {}}


def save_locs(locs):
    LOC_FILE.write_text(json.dumps(locs, indent=2))


def lookup(section, pub):
    """Find a saved location by the pub's name, or by one of its old names."""
    names = [pub["name"]] + pub.get("old_names", [])
    for n in names:
        if n in section:
            return section[n]
    return None


def geocode(query):
    """Search Nominatim, restricted to the Glasgow area. Postcodes work well too."""
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "gb",
                    "viewbox": "-4.45,55.95,-4.05,55.78", "bounded": 1},
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
        known = lookup(locs["manual"], p) or lookup(locs["auto"], p)
        if force or not known:
            for q in p["queries"]:
                res = geocode(q)
                time.sleep(1.1)  # be polite to the free service (max 1 request/second)
                if res:
                    locs["auto"][p["name"]] = res
                    changed = True
                    break
    if changed:
        save_locs(locs)
    return locs


def get_positions():
    locs = load_locs()
    out = {}
    for p in PUBS:
        pos = lookup(locs["manual"], p) or lookup(locs["auto"], p)
        if pos:
            out[p["name"]] = pos
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
skipped = state["skipped"]
started = state["started"]
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

    if not started:
        st.info("The crawl hasn't started yet. The organiser starts it from the Admin tab.")
    elif progress >= len(PUBS):
        st.success("That's every pub. The crawl is complete!")
    else:
        target = PUB_NAMES[progress]
        st.subheader(f"Next stop {progress + 1}/{len(PUBS)}: {target}")

        if target in positions:
            dest = positions[target]
            st.link_button(
                "Open in Google Maps",
                f"https://www.google.com/maps/dir/?api=1&destination={dest[0]},{dest[1]}&travelmode=walking",
            )

        if st.button(f"We've arrived at {target} – reveal the rule", type="primary"):
            set_progress(progress + 1)
            st.rerun()

        with st.popover("Skip this pub"):
            st.write(f"Skip **{target}** and move on to the next pub? Its rule stays hidden.")
            if st.button("Yes, skip it"):
                skip_pub()
                st.rerun()

    # rule for the pub we last arrived at (skipped pubs don't count)
    reached = [n for n in PUB_NAMES[:progress] if n not in skipped]
    if progress > 0 and PUB_NAMES[progress - 1] in skipped:
        st.caption(f"Skipped {PUB_NAMES[progress - 1]}.")
    if reached:
        here = reached[-1]
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
    if not [n for n in PUB_NAMES[:progress] if n not in skipped]:
        st.write("Nothing revealed yet. Get to the first pub!")
    for i in range(progress):
        n = PUB_NAMES[i]
        if n in skipped:
            continue
        entry = rules.get(n)
        with st.container(border=True):
            st.markdown(f"**{i + 1}. {n}**")
            st.write(entry["rule"] if entry else "No rule for this pub.")

# ---- Admin ----
with tab_admin:
    pw = st.text_input("Password", type="password")
    if not ADMIN_PASSWORD:
        st.info("Admin is locked because no admin_password has been set in Secrets.")
    elif ADMIN_PASSWORD and pw == ADMIN_PASSWORD:
        st.markdown("#### Start / stop the crawl")
        if started:
            st.success("The crawl is running.")
            if st.button("Pause the crawl"):
                set_started(False)
                st.rerun()
        else:
            st.warning("The crawl hasn't started. Nobody can reveal rules yet.")
            if st.button("Start the crawl", type="primary"):
                set_started(True)
                st.rerun()

        st.markdown("#### Crawl progress")
        c1, c2 = st.columns(2)
        if c1.button("Go back one pub"):
            set_progress(progress - 1)
            st.rerun()
        if c2.button("Skip forward one pub"):
            set_progress(progress + 1)
            st.rerun()

        st.markdown("#### All assignments")
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