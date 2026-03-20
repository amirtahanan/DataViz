import pandas as pd
import time
import json
from nba_api.stats.static import teams
from nba_api.stats.endpoints import commonteamroster, playerdashptpass, shotchartdetail


def build_assist_network(team_id: int) -> None:
    """Fetch roster + passing data and write assist_network.json."""
    roster = commonteamroster.CommonTeamRoster(team_id=team_id).get_data_frames()[0]

    nodes = []
    links = []

    print("Fetching roster and building nodes...")
    for _, row in roster.iterrows():
        nodes.append({
            "id": row['PLAYER'],
            "group": 3,
            "team": "DAL",
            "ppg": 0,
        })

    print("Fetching passing data for each player (pausing between requests to avoid rate limits)...")

    for _, row in roster.iterrows():
        player_id = row['PLAYER_ID']
        player_name = row['PLAYER']

        try:
            passing_stats = playerdashptpass.PlayerDashPtPass(team_id=team_id, player_id=player_id)
            passes_made_df = passing_stats.passes_made.get_data_frame()

            assists_only = passes_made_df[passes_made_df['AST'] > 5]

            for _, pass_row in assists_only.iterrows():
                links.append({
                    "source": player_name,
                    "target": pass_row['PASS_TO'],
                    "value": pass_row['AST'],
                })

            time.sleep(1)

        except Exception as e:
            print(f"Skipped {player_name} or encountered an error: {e}")

    network_data = {"nodes": nodes, "links": links}

    with open('assist_network.json', 'w') as f:
        json.dump(network_data, f, indent=4)

    print("Done! Data saved to assist_network.json")


def build_shot_heatmap(team_id: int, season: str = "2023-24") -> None:
    """Fetch made shots for the team and write shot_heatmap.json as binned counts."""
    print("Fetching shot chart data for heatmap...")

    # Team-level shot chart (all players), regular season
    sc = shotchartdetail.ShotChartDetail(
        team_id=team_id,
        player_id=0,
        season_type_all_star="Regular Season",
        season_nullable=season,
    )
    shots_df = sc.get_data_frames()[0]

    # Keep only made shots with court coordinates
    made = shots_df[shots_df["SHOT_MADE_FLAG"] == 1][["LOC_X", "LOC_Y"]]

    # Simple square binning on NBA shot chart coordinates
    bin_size = 20  # units in NBA loc coordinates
    bins = {}

    for _, row in made.iterrows():
        x = row["LOC_X"]
        y = row["LOC_Y"]
        bx = int(x // bin_size)
        by = int(y // bin_size)
        key = (bx, by)
        bins[key] = bins.get(key, 0) + 1

    heatmap = []
    for (bx, by), count in bins.items():
        heatmap.append({
            "x": bx * bin_size + bin_size / 2,
            "y": by * bin_size + bin_size / 2,
            "count": int(count),
            "bin_size": bin_size,
        })

    with open("shot_heatmap.json", "w") as f:
        json.dump(heatmap, f, indent=4)

    print("Done! Data saved to shot_heatmap.json")


if __name__ == "__main__":
    # Use the Mavericks (same as assist network)
    team_info = teams.find_teams_by_full_name("Mavericks")[0]
    team_id = team_info["id"]

    build_assist_network(team_id)
    build_shot_heatmap(team_id)