-- version 2
-- keeps every announced ranked match so activity covers all ranks
-- safe to run twice since everything is if not exists

BEGIN;

-- announcements
-- every ranked match the match server announces at any rank
-- kept for activity even when the result isnt tracked
-- names only since most lower ranked players have no playfab id here
CREATE TABLE IF NOT EXISTS announcements (
    match_id TEXT PRIMARY KEY, -- match server id
    started_at TEXT NOT NULL,
    player_one TEXT NOT NULL,
    player_two TEXT NOT NULL,
    player_one_lp INTEGER,
    player_two_lp INTEGER
);
CREATE INDEX IF NOT EXISTS idx_announcements_started ON announcements (started_at);

-- every ranked game for the activity view
-- announcements are complete from when they started being kept
-- recorded matches fill in the time before that
CREATE VIEW IF NOT EXISTS v_activity AS
SELECT started_at, player_one, player_two, player_one_lp, player_two_lp
FROM announcements
UNION ALL
SELECT m.started_at, p0.display_name, p1.display_name, p0.league_points, p1.league_points
FROM v_matches m
JOIN match_players p0 ON p0.match_id = m.id AND p0.slot = 0
JOIN match_players p1 ON p1.match_id = m.id AND p1.slot = 1
WHERE m.started_at < COALESCE((SELECT MIN(started_at) FROM announcements), '9999');

PRAGMA user_version = 2;

COMMIT;
