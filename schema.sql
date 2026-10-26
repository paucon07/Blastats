-- blastats sqlite schema
-- builds blastats db in the data folder (safe to rerun)

-- readers and the poller dont block each other
-- saved in the file so it only needs setting once
PRAGMA journal_mode = WAL;
-- off by default so every connection has to turn it on
PRAGMA foreign_keys = ON;

-- players
-- one row per playfab account
-- legacy ids with a name prefix have no real playfab account
CREATE TABLE IF NOT EXISTS players (
    player_id TEXT PRIMARY KEY, -- playfab id
    display_name TEXT NOT NULL,
    source TEXT NOT NULL, -- leaderboard or stats_leaderboard
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    account_created TEXT,
    account_checked_at TEXT, -- last lookup even if it failed
    stats_updated_at TEXT,
    stats_backfilled_at TEXT -- last off ladder lookup even if it failed
);
-- names arent unique since players can rename (unlikely but still)
-- nocase so lookups ignore case like find_player
CREATE INDEX IF NOT EXISTS idx_players_name ON players (display_name COLLATE NOCASE);

-- latest value from each playfab stat ladder
CREATE TABLE IF NOT EXISTS player_stats (
    player_id TEXT NOT NULL REFERENCES players (player_id) ON DELETE CASCADE,
    stat_name TEXT NOT NULL, -- ELO_RATING or ROUNDS_WON and so on
    value INTEGER NOT NULL,
    PRIMARY KEY (player_id, stat_name)
) WITHOUT ROWID;

-- seasons
-- every lp season version the poller has seen
-- status marks which season is current and which is previous
CREATE TABLE IF NOT EXISTS seasons (
    version TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    status TEXT UNIQUE CHECK (status IN ('current', 'previous')), -- null for older seasons
    updated_at TEXT -- last leaderboard poll for the season
);

-- season lp ladders
-- rows for a season are replaced in full each leaderboard poll
-- season lp is read from here so a season reset needs no clean up
CREATE TABLE IF NOT EXISTS leaderboard_entries (
    season_version TEXT NOT NULL REFERENCES seasons (version) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 1), -- starts at 1 unlike the server
    player_id TEXT REFERENCES players (player_id), -- null when the server sent no id
    display_name TEXT NOT NULL,
    value INTEGER NOT NULL, -- league points
    secondary_value INTEGER,
    formatted_value TEXT,
    PRIMARY KEY (season_version, position)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_leaderboard_player ON leaderboard_entries (player_id);

-- weapons
-- loadouts use internal ids and damage uses display names
-- both map to one display name here like WEAPON_NAMES in the api
CREATE TABLE IF NOT EXISTS weapons (
    name TEXT PRIMARY KEY COLLATE NOCASE, -- display name with nocase since frostbyte casing varies
    internal_id TEXT UNIQUE -- null until the weapon is mapped
);

-- matches
-- one row per real game and only valid best of 3 results are saved
-- spectated and announced copies are paired once when saved instead of on every request
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY,
    announcement_id TEXT UNIQUE, -- match server id from the poller
    spectate_id TEXT UNIQUE, -- match id the mod saw and missing on older rows
    started_at TEXT NOT NULL, -- announcement time when there is one
    ended_at TEXT, -- spectated only
    spectated INTEGER NOT NULL DEFAULT 0 CHECK (spectated IN (0, 1)),
    region TEXT,
    end_reason TEXT,
    winner_slot INTEGER NOT NULL CHECK (winner_slot IN (0, 1)), -- as recorded before corrections
    round_count INTEGER NOT NULL CHECK (round_count IN (2, 3)),
    -- needs at least one source
    CHECK (announcement_id IS NOT NULL OR spectated = 1)
);
-- match log and activity order
CREATE INDEX IF NOT EXISTS idx_matches_started ON matches (started_at);
-- weapon stats only read spectated matches
CREATE INDEX IF NOT EXISTS idx_matches_spectated ON matches (started_at) WHERE spectated = 1;

-- both players in a match
-- names are kept as they were since players can rename
CREATE TABLE IF NOT EXISTS match_players (
    match_id INTEGER NOT NULL REFERENCES matches (id) ON DELETE CASCADE,
    slot INTEGER NOT NULL CHECK (slot IN (0, 1)),
    player_id TEXT REFERENCES players (player_id), -- null when the name matched no tracked player
    display_name TEXT NOT NULL,
    league_points INTEGER,
    elo INTEGER, -- elo before the match
    rounds_start INTEGER, -- lifetime rounds won before the match for rectify
    -- spectated only
    network_id INTEGER, -- photon actor id where 1 is the host
    character_id INTEGER,
    character TEXT, -- portrait sprite in the avatars folder
    character_name TEXT,
    ping INTEGER, -- average ms
    PRIMARY KEY (match_id, slot)
) WITHOUT ROWID;
-- profiles and head to head look matches up by player
CREATE INDEX IF NOT EXISTS idx_match_players_player ON match_players (player_id);
CREATE INDEX IF NOT EXISTS idx_match_players_name ON match_players (display_name COLLATE NOCASE);

-- weapons each player had on the scoreboard in spectated matches
CREATE TABLE IF NOT EXISTS match_loadouts (
    match_id INTEGER NOT NULL,
    slot INTEGER NOT NULL,
    position INTEGER NOT NULL, -- order in the loadout
    weapon TEXT NOT NULL REFERENCES weapons (name),
    attachment TEXT,
    PRIMARY KEY (match_id, slot, position),
    FOREIGN KEY (match_id, slot) REFERENCES match_players (match_id, slot) ON DELETE CASCADE
) WITHOUT ROWID;
-- weapon meta counts loadouts per weapon
CREATE INDEX IF NOT EXISTS idx_loadouts_weapon ON match_loadouts (weapon);

-- rounds
-- poller only matches have the round order guessed from the score
CREATE TABLE IF NOT EXISTS rounds (
    match_id INTEGER NOT NULL REFERENCES matches (id) ON DELETE CASCADE,
    round_number INTEGER NOT NULL CHECK (round_number BETWEEN 1 AND 3),
    winner_slot INTEGER NOT NULL CHECK (winner_slot IN (0, 1)), -- as recorded
    -- spectated only
    duration_seconds REAL,
    forfeit_slot INTEGER CHECK (forfeit_slot IN (0, 1)), -- null unless someone forfeit
    slot0_health INTEGER, -- health left at the end of the round
    slot1_health INTEGER,
    PRIMARY KEY (match_id, round_number)
) WITHOUT ROWID;

-- per round totals for each player on spectated matches
-- stored against the player who took it so dealt damage comes from the other slot
CREATE TABLE IF NOT EXISTS round_stats (
    match_id INTEGER NOT NULL,
    round_number INTEGER NOT NULL,
    receiver_slot INTEGER NOT NULL CHECK (receiver_slot IN (0, 1)),
    stat_type TEXT NOT NULL, -- damage or grab count
    source TEXT NOT NULL, -- weapon name, throw or cage
    value REAL NOT NULL,
    PRIMARY KEY (match_id, round_number, receiver_slot, stat_type, source),
    FOREIGN KEY (match_id, round_number) REFERENCES rounds (match_id, round_number) ON DELETE CASCADE
) WITHOUT ROWID;

-- per hit damage timeline for the replay view
CREATE TABLE IF NOT EXISTS round_events (
    match_id INTEGER NOT NULL,
    round_number INTEGER NOT NULL,
    seq INTEGER NOT NULL, -- order in the round
    t REAL NOT NULL, -- seconds into the round
    weapon TEXT NOT NULL,
    receiver_slot INTEGER NOT NULL CHECK (receiver_slot IN (0, 1)),
    dmg REAL NOT NULL,
    PRIMARY KEY (match_id, round_number, seq),
    FOREIGN KEY (match_id, round_number) REFERENCES rounds (match_id, round_number) ON DELETE CASCADE
) WITHOUT ROWID;

-- corrections
-- a row flips the recorded winner unless exclude is set
-- rectify rebuilds the auto rows and leaves manual rows alone
CREATE TABLE IF NOT EXISTS match_corrections (
    match_id INTEGER PRIMARY KEY REFERENCES matches (id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'auto' CHECK (source IN ('auto', 'manual')),
    exclude INTEGER NOT NULL DEFAULT 0 CHECK (exclude IN (0, 1)),
    rule TEXT, -- strong or silent_upset
    winner_elo_move INTEGER, -- elo moves are only there to verify flips
    loser_elo_move INTEGER,
    note TEXT,
    created_at TEXT NOT NULL
);

-- tournaments
-- added manually
CREATE TABLE IF NOT EXISTS tournaments (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS tournament_placements (
    tournament_id INTEGER NOT NULL REFERENCES tournaments (id) ON DELETE CASCADE,
    display_name TEXT NOT NULL,
    player_id TEXT REFERENCES players (player_id), -- null when the name matched no tracked player
    place INTEGER NOT NULL CHECK (place >= 1),
    prize_money REAL NOT NULL DEFAULT 0 CHECK (prize_money >= 0), -- usd
    PRIMARY KEY (tournament_id, display_name)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_placements_name ON tournament_placements (display_name COLLATE NOCASE);

-- views
-- matches as the site shows them with corrections applied
CREATE VIEW IF NOT EXISTS v_matches AS
SELECT m.id, m.announcement_id, m.spectate_id, m.started_at, m.ended_at,
    m.spectated, m.region, m.end_reason, m.round_count,
    CASE WHEN c.match_id IS NULL THEN m.winner_slot ELSE 1 - m.winner_slot END AS winner_slot,
    CASE m.round_count WHEN 2 THEN '2-0' ELSE '2-1' END AS score,
    c.match_id IS NOT NULL AS corrected
FROM matches m
LEFT JOIN match_corrections c ON c.match_id = m.id AND m.spectated = 0
-- drop excluded matches
WHERE c.exclude IS NOT 1;

-- rounds with every round winner swapped on a corrected match
CREATE VIEW IF NOT EXISTS v_rounds AS
SELECT r.match_id, r.round_number,
    CASE WHEN vm.corrected THEN 1 - r.winner_slot ELSE r.winner_slot END AS winner_slot,
    r.duration_seconds, r.forfeit_slot, r.slot0_health, r.slot1_health
FROM rounds r
JOIN v_matches vm ON vm.id = r.match_id;

-- one row per player per match from their side
-- profiles and streaks and head to head all start here
CREATE VIEW IF NOT EXISTS v_player_matches AS
SELECT vm.id AS match_id, vm.started_at, vm.spectated, vm.score, vm.corrected,
    me.slot, me.player_id, me.display_name, me.league_points, me.elo,
    opp.player_id AS opponent_id, opp.display_name AS opponent_name,
    opp.league_points AS opponent_lp,
    vm.winner_slot = me.slot AS won
FROM v_matches vm
JOIN match_players me ON me.match_id = vm.id
JOIN match_players opp ON opp.match_id = vm.id AND opp.slot = 1 - me.slot;

-- players with season lp and elo alongside
CREATE VIEW IF NOT EXISTS v_player_standings AS
SELECT p.player_id, p.display_name, p.first_seen, p.last_seen, p.account_created,
    cur.value AS current_season_lp, cur.position AS current_season_position,
    prev.value AS previous_season_lp, prev.position AS previous_season_position,
    elo.value AS elo
FROM players p
LEFT JOIN leaderboard_entries cur ON cur.player_id = p.player_id
    AND cur.season_version = (SELECT version FROM seasons WHERE status = 'current')
LEFT JOIN leaderboard_entries prev ON prev.player_id = p.player_id
    AND prev.season_version = (SELECT version FROM seasons WHERE status = 'previous')
LEFT JOIN player_stats elo ON elo.player_id = p.player_id AND elo.stat_name = 'ELO_RATING';

-- total prize money per player
CREATE VIEW IF NOT EXISTS v_earnings AS
SELECT MIN(display_name) AS display_name, SUM(prize_money) AS total_earnings
FROM tournament_placements
WHERE prize_money > 0
GROUP BY display_name COLLATE NOCASE;

-- bump when the schema changes so old databases can be migrated
PRAGMA user_version = 1;
