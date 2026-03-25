import pandas as pd
import time
import json
from nba_api.stats.static import teams
from nba_api.stats.endpoints import commonteamroster, shotchartdetail, playerdashptpass


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


def build_shot_heatmap(team_id: int, season: str = "2025-26") -> dict:
    """Fetch made shots for one team and return binned shot counts by player."""
    print(f"Fetching shot chart data for team {team_id}...")

    # Team-level shot chart (all players), regular season
    sc = shotchartdetail.ShotChartDetail(
        team_id=team_id,
        player_id=0,
        season_type_all_star="Regular Season",
        season_nullable=season,
    )
    shots_df = sc.get_data_frames()[0]

    # Keep only made shots with court coordinates
    made = shots_df[shots_df["SHOT_MADE_FLAG"] == 1][["LOC_X", "LOC_Y", "PLAYER_NAME"]]

    # Simple square binning on NBA shot chart coordinates
    bin_size = 20  # units in NBA loc coordinates
    bins_by_player = {}

    for _, row in made.iterrows():
        player_name = row["PLAYER_NAME"]
        if player_name not in bins_by_player:
            bins_by_player[player_name] = {}

        x = row["LOC_X"]
        y = row["LOC_Y"]
        bx = int(x // bin_size)
        by = int(y // bin_size)
        key = (bx, by)
        bins_by_player[player_name][key] = bins_by_player[player_name].get(key, 0) + 1

    # Build output with overall team heatmap + per-player breakdown
    team_bins = {}
    for player_name, player_bins in bins_by_player.items():
        for key, count in player_bins.items():
            team_bins[key] = team_bins.get(key, 0) + count

    heatmap_team = [
        {
            "x": bx * bin_size + bin_size / 2,
            "y": by * bin_size + bin_size / 2,
            "count": int(count),
            "bin_size": bin_size,
        }
        for (bx, by), count in team_bins.items()
    ]

    heatmap_by_player = {}
    for player_name, player_bins in bins_by_player.items():
        heatmap_by_player[player_name] = [
            {
                "x": bx * bin_size + bin_size / 2,
                "y": by * bin_size + bin_size / 2,
                "count": int(count),
                "bin_size": bin_size,
            }
            for (bx, by), count in player_bins.items()
        ]

    return {
        "team": heatmap_team,
        "by_player": heatmap_by_player,
    }


def build_all_teams_datasets(season: str = "2025-26") -> None:
    """Build assist and shot datasets for all NBA teams."""
    team_list = teams.get_teams()

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

    print("Building all-team datasets. This can take several minutes...")
    for t in team_list:
        team_id = t["id"]
        team_abbr = t["abbreviation"]
        print(f"\n--- {team_abbr} ({team_id}) ---")

        try:
            assist_payload["data"][team_abbr] = build_assist_network(team_id, team_abbr)
        except Exception as e:
            print(f"Failed assist network for {team_abbr}: {e}")
            assist_payload["data"][team_abbr] = {"nodes": [], "links": []}

        try:
            shot_payload["data"][team_abbr] = build_shot_heatmap(team_id, season=season)
        except Exception as e:
            print(f"Failed shot heatmap for {team_abbr}: {e}")
            shot_payload["data"][team_abbr] = []

        time.sleep(0.8)

    with open("assist_network_all_teams.json", "w") as f:
        json.dump(assist_payload, f, indent=4)

    with open("shot_heatmap_all_teams.json", "w") as f:
        json.dump(shot_payload, f, indent=4)

    # Keep legacy single-team files for quick local previews (default DAL when available).
    default_team = "DAL" if "DAL" in assist_payload["data"] else assist_payload["teams"][0]["abbreviation"]
    with open("assist_network.json", "w") as f:
        json.dump(assist_payload["data"][default_team], f, indent=4)

    with open("shot_heatmap.json", "w") as f:
        json.dump(shot_payload["data"][default_team], f, indent=4)

    print("\nDone! Wrote assist_network_all_teams.json and shot_heatmap_all_teams.json")


if __name__ == "__main__":
    build_all_teams_datasets(season="2025-26")