import pandas as pd
import time
import json
import argparse
from nba_api.stats.static import teams
from nba_api.stats.endpoints import commonteamroster, shotchartdetail, playerdashptpass
from nba_api.live.nba.endpoints import playbyplay


def normalize_player_name(name: str) -> str:
    """Convert LAST, FIRST style names to FIRST LAST to match roster names."""
    if not isinstance(name, str):
        return str(name)
    if "," not in name:
        return name.strip()
    parts = [p.strip() for p in name.split(",") if p.strip()]
    if len(parts) != 2:
        return name.strip()
    return f"{parts[1]} {parts[0]}"


def add_shot_to_bins(bin_store: dict, x: float, y: float, bin_size: int) -> None:
    """Increment a single shot into a square bin map."""
    bx = int(x // bin_size)
    by = int(y // bin_size)
    key = (bx, by)
    bin_store[key] = bin_store.get(key, 0) + 1


def serialize_bins(bin_store: dict, bin_size: int) -> list:
    """Convert bin dictionaries into the frontend heatmap format."""
    return [
        {
            "x": bx * bin_size + bin_size / 2,
            "y": by * bin_size + bin_size / 2,
            "count": int(count),
            "bin_size": bin_size,
        }
        for (bx, by), count in bin_store.items()
    ]


def load_assist_lookup(game_id: str, playbyplay_cache: dict, player_id_to_name: dict) -> dict:
    """Fetch once per game and cache assister metadata keyed by event number."""
    if game_id in playbyplay_cache:
        return playbyplay_cache[game_id]

    assist_lookup = {}
    last_error = None
    for attempt in range(3):
        try:
            game_actions = playbyplay.PlayByPlay(game_id=game_id, timeout=20).get_dict().get("game", {}).get("actions", [])

            for action in game_actions:
                if not action.get("isFieldGoal"):
                    continue
                if action.get("shotResult") != "Made":
                    continue
                if not action.get("assistPersonId"):
                    continue

                shot_action_number = action.get("shotActionNumber")
                action_number = action.get("actionNumber")
                if shot_action_number is None and action_number is None:
                    continue

                assister_id = action.get("assistPersonId")
                assister_name = player_id_to_name.get(assister_id)
                if not assister_name:
                    assister_name = action.get("assistPlayerName") or action.get("assistPlayerNameInitial")
                shooter_name = action.get("playerName") or action.get("playerNameI")

                assist_entry = {
                    "shooter": normalize_player_name(shooter_name or ""),
                    "assister": normalize_player_name(assister_name or ""),
                }

                if shot_action_number is not None:
                    assist_lookup[int(shot_action_number)] = assist_entry
                if action_number is not None:
                    assist_lookup.setdefault(int(action_number), assist_entry)

            last_error = None
            break
        except Exception as e:
            last_error = e
            if attempt < 2:
                time.sleep(1.0 + attempt)

    if last_error is not None:
        print(f"Play-by-play unavailable for {game_id}: {last_error}")

    playbyplay_cache[game_id] = assist_lookup
    time.sleep(0.35)
    return assist_lookup


def build_assist_network(team_id: int, team_abbreviation: str) -> dict:
    """Fetch roster + per-player passing edges and return nodes/links for one team."""
    roster = commonteamroster.CommonTeamRoster(team_id=team_id).get_data_frames()[0]

    nodes = []
    edge_totals = {}
    roster_names = set()
    normalized_to_canonical = {}

    print(f"Building nodes for {team_abbreviation}...")
    for _, row in roster.iterrows():
        player_name = row["PLAYER"]
        roster_names.add(player_name)
        normalized_to_canonical[normalize_player_name(player_name)] = player_name
        nodes.append({
            "id": player_name,
            "player_id": int(row["PLAYER_ID"]),
            "group": 3,
            "team": team_abbreviation,
            "position": row.get("POSITION", "Unknown"),
            "ppg": 0,
        })

    print(f"Fetching passing edges for {team_abbreviation} by player...")
    for _, row in roster.iterrows():
        source_name = row["PLAYER"]
        source_id = int(row["PLAYER_ID"])

        try:
            passing = playerdashptpass.PlayerDashPtPass(team_id=team_id, player_id=source_id)
            passes_made_df = passing.passes_made.get_data_frame()
        except Exception as e:
            print(f"Skipped {source_name} ({team_abbreviation}) due to endpoint error: {e}")
            continue

        if "AST" not in passes_made_df.columns or "PASS_TO" not in passes_made_df.columns:
            continue

        assists_only = passes_made_df[passes_made_df["AST"] > 0]
        for _, pass_row in assists_only.iterrows():
            raw_target = pass_row["PASS_TO"]
            target_norm = normalize_player_name(raw_target)
            target_name = normalized_to_canonical.get(target_norm, target_norm)

            if target_name not in roster_names:
                continue

            ast_value = int(pass_row["AST"])
            key = (source_name, target_name)
            edge_totals[key] = edge_totals.get(key, 0) + ast_value

        # Small delay helps avoid rate limiting when iterating full league.
        time.sleep(0.35)

    links = [
        {"source": source, "target": target, "value": value}
        for (source, target), value in edge_totals.items()
    ]

    print(f"{team_abbreviation}: {len(nodes)} nodes, {len(links)} assist links")

    return {"nodes": nodes, "links": links}


def build_shot_heatmap(team_id: int, season: str = "2025-26", playbyplay_cache: dict | None = None) -> dict:
    """Fetch made shots for one team and return overall, assisted, and passer-created shot bins."""
    print(f"Fetching shot chart data for team {team_id}...")

    if playbyplay_cache is None:
        playbyplay_cache = {}

    roster_df = commonteamroster.CommonTeamRoster(team_id=team_id).get_data_frames()[0]
    player_id_to_name = {
        int(row["PLAYER_ID"]): normalize_player_name(row["PLAYER"])
        for _, row in roster_df.iterrows()
    }

    # Team-level shot chart (all players), regular season
    sc = shotchartdetail.ShotChartDetail(
        team_id=team_id,
        player_id=0,
        season_type_all_star="Regular Season",
        season_nullable=season,
    )
    shots_df = sc.get_data_frames()[0]

    # Keep only made shots with court coordinates
    made = shots_df[shots_df["SHOT_MADE_FLAG"] == 1][["GAME_ID", "GAME_EVENT_ID", "LOC_X", "LOC_Y", "PLAYER_NAME"]].copy()
    made["PLAYER_NAME"] = made["PLAYER_NAME"].apply(normalize_player_name)

    # Simple square binning on NBA shot chart coordinates
    bin_size = 20  # units in NBA loc coordinates
    team_bins = {}
    bins_by_player = {}
    assisted_team_bins = {}
    assisted_by_player = {}
    bins_by_assister = {}
    bins_by_pair = {}

    assist_lookups = {}
    for game_id in made["GAME_ID"].dropna().astype(str).unique():
        assist_lookups[game_id] = load_assist_lookup(game_id, playbyplay_cache, player_id_to_name)

    for _, row in made.iterrows():
        player_name = row["PLAYER_NAME"]
        if player_name not in bins_by_player:
            bins_by_player[player_name] = {}

        x = row["LOC_X"]
        y = row["LOC_Y"]
        add_shot_to_bins(team_bins, x, y, bin_size)
        add_shot_to_bins(bins_by_player[player_name], x, y, bin_size)

        assister = None
        try:
            event_num = int(row["GAME_EVENT_ID"])
            game_lookup = assist_lookups.get(str(row["GAME_ID"]), {})
            event_info = game_lookup.get(event_num)
            if event_info:
                assister = event_info.get("assister")
        except (TypeError, ValueError):
            assister = None

        if assister:
            add_shot_to_bins(assisted_team_bins, x, y, bin_size)

            if player_name not in assisted_by_player:
                assisted_by_player[player_name] = {}
            add_shot_to_bins(assisted_by_player[player_name], x, y, bin_size)

            if assister not in bins_by_assister:
                bins_by_assister[assister] = {}
            add_shot_to_bins(bins_by_assister[assister], x, y, bin_size)

            pair_key = f"{assister} -> {player_name}"
            if pair_key not in bins_by_pair:
                bins_by_pair[pair_key] = {}
            add_shot_to_bins(bins_by_pair[pair_key], x, y, bin_size)

    # Build output with overall team heatmap + per-player breakdown
    heatmap_team = serialize_bins(team_bins, bin_size)

    heatmap_by_player = {}
    for player_name, player_bins in bins_by_player.items():
        heatmap_by_player[player_name] = serialize_bins(player_bins, bin_size)

    heatmap_assisted_by_player = {}
    for player_name, player_bins in assisted_by_player.items():
        heatmap_assisted_by_player[player_name] = serialize_bins(player_bins, bin_size)

    heatmap_by_assister = {}
    for assister, assister_bins in bins_by_assister.items():
        heatmap_by_assister[assister] = serialize_bins(assister_bins, bin_size)

    heatmap_by_pair = {}
    for pair_key, pair_bins in bins_by_pair.items():
        heatmap_by_pair[pair_key] = serialize_bins(pair_bins, bin_size)

    return {
        "team": heatmap_team,
        "by_player": heatmap_by_player,
        "assisted_team": serialize_bins(assisted_team_bins, bin_size),
        "assisted_by_player": heatmap_assisted_by_player,
        "by_assister": heatmap_by_assister,
        "by_assist_pair": heatmap_by_pair,
        "meta": {
            "season": season,
            "bin_size": bin_size,
            "assisted_pairs": len(heatmap_by_pair),
        },
    }


def build_team_datasets(team_info: dict, season: str, playbyplay_cache: dict) -> tuple[dict, dict]:
    """Build assist and shot payloads for a single team."""
    team_id = team_info["id"]
    team_abbr = team_info["abbreviation"]

    print(f"\n--- {team_abbr} ({team_id}) ---")

    try:
        assist_data = build_assist_network(team_id, team_abbr)
    except Exception as e:
        print(f"Failed assist network for {team_abbr}: {e}")
        assist_data = {"nodes": [], "links": []}

    try:
        shot_data = build_shot_heatmap(team_id, season=season, playbyplay_cache=playbyplay_cache)
    except Exception as e:
        print(f"Failed shot heatmap for {team_abbr}: {e}")
        shot_data = {"team": [], "by_player": {}}

    return assist_data, shot_data


def write_datasets(assist_payload: dict, shot_payload: dict) -> None:
    """Write combined payloads plus legacy single-team preview files."""
    with open("assist_network_all_teams.json", "w") as f:
        json.dump(assist_payload, f, indent=4)

    with open("shot_heatmap_all_teams.json", "w") as f:
        json.dump(shot_payload, f, indent=4)

    default_team = "DAL" if "DAL" in assist_payload["data"] else assist_payload["teams"][0]["abbreviation"]
    with open("assist_network.json", "w") as f:
        json.dump(assist_payload["data"][default_team], f, indent=4)

    with open("shot_heatmap.json", "w") as f:
        json.dump(shot_payload["data"][default_team], f, indent=4)


def resolve_team_list(team_selector: str | None) -> list[dict]:
    """Return all teams or a single selected team from an abbreviation/full name/id."""
    team_list = teams.get_teams()
    if not team_selector or team_selector.lower() == "all":
        return team_list

    query = team_selector.strip().lower()
    for team in team_list:
        if query in {
            str(team["id"]).lower(),
            team["abbreviation"].lower(),
            team["full_name"].lower(),
            team["nickname"].lower(),
            team["city"].lower(),
        }:
            return [team]

    raise ValueError(f"Unknown team selector: {team_selector}")


def build_all_teams_datasets(season: str = "2025-26", team_selector: str | None = None) -> None:
    """Build assist and shot datasets for all teams or one selected team."""
    team_list = resolve_team_list(team_selector)
    playbyplay_cache = {}

    assist_payload = {
        "teams": [
            {
                "id": t["id"],
                "abbreviation": t["abbreviation"],
                "full_name": t["full_name"],
            }
            for t in team_list
        ],
        "data": {},
    }
    shot_payload = {
        "teams": assist_payload["teams"],
        "data": {},
    }

    if len(team_list) == 1:
        print(f"Building demo dataset for {team_list[0]['abbreviation']} only...")
    else:
        print("Building all-team datasets. This can take several minutes...")

    for team_info in team_list:
        assist_data, shot_data = build_team_datasets(team_info, season, playbyplay_cache)
        assist_payload["data"][team_info["abbreviation"]] = assist_data
        shot_payload["data"][team_info["abbreviation"]] = shot_data

        time.sleep(0.8)

    write_datasets(assist_payload, shot_payload)
    print("\nDone! Wrote assist_network_all_teams.json and shot_heatmap_all_teams.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build NBA assist and shot datasets.")
    parser.add_argument(
        "team",
        nargs="?",
        default="all",
        help="Team abbreviation, team id, or full team name. Use 'all' for the full league.",
    )
    parser.add_argument(
        "--season",
        default="2025-26",
        help="Season string in NBA format, for example 2025-26.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    build_all_teams_datasets(season=args.season, team_selector=args.team)