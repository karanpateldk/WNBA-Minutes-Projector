"""
Static WNBA roster and depth chart data used when scraping fails.
Minutes are season averages — 2025 season rosters.
"""

TEAMS = {
    "New York Liberty":        "NYL",
    "Las Vegas Aces":          "LVA",
    "Connecticut Sun":         "CON",
    "Seattle Storm":           "SEA",
    "Chicago Sky":             "CHI",
    "Minnesota Lynx":          "MIN",
    "Los Angeles Sparks":      "LAS",
    "Phoenix Mercury":         "PHX",
    "Atlanta Dream":           "ATL",
    "Washington Mystics":      "WAS",
    "Dallas Wings":            "DAL",
    "Indiana Fever":           "IND",
    "Golden State Valkyries":  "GSV",
    "Portland Fire":           "POR",
    "Toronto Tempo":           "TOR",
}

TEAM_ABBREV_TO_NAME = {v: k for k, v in TEAMS.items()}

ROSTERS = {
    "New York Liberty": {
        "Sabrina Ionescu":      {"pos": "G",   "avg_min": 32.0, "role": "starter", "depth": 1},
        "Breanna Stewart":      {"pos": "F",   "avg_min": 31.5, "role": "starter", "depth": 1},
        "Jonquel Jones":        {"pos": "C",   "avg_min": 29.0, "role": "starter", "depth": 1},
        "Leonie Fiebich":       {"pos": "G",   "avg_min": 28.5, "role": "starter", "depth": 1},
        "Kayla Thornton":       {"pos": "F",   "avg_min": 22.0, "role": "starter", "depth": 1},
        "Courtney Vandersloot": {"pos": "G",   "avg_min": 18.0, "role": "bench",   "depth": 2},
        "Nyara Sabally":        {"pos": "F",   "avg_min": 14.0, "role": "bench",   "depth": 2},
        "Marine Johannes":      {"pos": "G",   "avg_min": 13.0, "role": "bench",   "depth": 2},
        "Betnijah Laney":       {"pos": "G",   "avg_min": 11.0, "role": "bench",   "depth": 2},
        "Kennedy Burke":        {"pos": "G",   "avg_min":  8.0, "role": "bench",   "depth": 3},
        "DiJonai Carrington":   {"pos": "G",   "avg_min":  5.0, "role": "bench",   "depth": 3},
    },
    "Las Vegas Aces": {
        "A'ja Wilson":          {"pos": "C",   "avg_min": 32.0, "role": "starter", "depth": 1},
        "Chelsea Gray":         {"pos": "G",   "avg_min": 28.5, "role": "starter", "depth": 1},
        "Kelsey Plum":          {"pos": "G",   "avg_min": 29.0, "role": "starter", "depth": 1},
        "Jackie Young":         {"pos": "G",   "avg_min": 30.0, "role": "starter", "depth": 1},
        "Alysha Clark":         {"pos": "F",   "avg_min": 22.0, "role": "starter", "depth": 1},
        "Tiffany Hayes":        {"pos": "G",   "avg_min": 18.0, "role": "bench",   "depth": 2},
        "Kierstan Bell":        {"pos": "G",   "avg_min": 14.0, "role": "bench",   "depth": 2},
        "Iliana Rupert":        {"pos": "F",   "avg_min": 12.0, "role": "bench",   "depth": 2},
        "Kate Martin":          {"pos": "G",   "avg_min":  9.0, "role": "bench",   "depth": 3},
    },
    "Connecticut Sun": {
        "Alyssa Thomas":        {"pos": "F",   "avg_min": 33.0, "role": "starter", "depth": 1},
        "DeWanna Bonner":       {"pos": "F",   "avg_min": 29.0, "role": "starter", "depth": 1},
        "DiJonai Carrington":   {"pos": "G",   "avg_min": 28.0, "role": "starter", "depth": 1},
        "Tyasha Harris":        {"pos": "G",   "avg_min": 26.0, "role": "starter", "depth": 1},
        "Brionna Jones":        {"pos": "C",   "avg_min": 24.0, "role": "starter", "depth": 1},
        "Rachel Banham":        {"pos": "G",   "avg_min": 17.0, "role": "bench",   "depth": 2},
        "Natisha Hiedeman":     {"pos": "G",   "avg_min": 14.0, "role": "bench",   "depth": 2},
        "Olivia Nelson-Ododa":  {"pos": "C",   "avg_min": 12.0, "role": "bench",   "depth": 2},
        "Tiffany Mitchell":     {"pos": "G",   "avg_min": 10.0, "role": "bench",   "depth": 3},
    },
    "Seattle Storm": {
        "Nneka Ogwumike":       {"pos": "F",   "avg_min": 30.0, "role": "starter", "depth": 1},
        "Jewell Loyd":          {"pos": "G",   "avg_min": 30.5, "role": "starter", "depth": 1},
        "Gabby Williams":       {"pos": "F",   "avg_min": 28.0, "role": "starter", "depth": 1},
        "Skylar Diggins-Smith": {"pos": "G",   "avg_min": 28.0, "role": "starter", "depth": 1},
        "Ezi Magbegor":         {"pos": "C",   "avg_min": 26.0, "role": "starter", "depth": 1},
        "Jordan Horston":       {"pos": "G",   "avg_min": 16.0, "role": "bench",   "depth": 2},
        "Sami Whitcomb":        {"pos": "G",   "avg_min": 14.0, "role": "bench",   "depth": 2},
        "Mercedes Russell":     {"pos": "C",   "avg_min": 12.0, "role": "bench",   "depth": 2},
        "Victoria Vivians":     {"pos": "G",   "avg_min":  9.0, "role": "bench",   "depth": 3},
    },
    "Chicago Sky": {
        "Kamilla Cardoso":      {"pos": "C",   "avg_min": 28.0, "role": "starter", "depth": 1},
        "Chennedy Carter":      {"pos": "G",   "avg_min": 30.0, "role": "starter", "depth": 1},
        "Angel Reese":          {"pos": "F",   "avg_min": 29.0, "role": "starter", "depth": 1},
        "Marina Mabrey":        {"pos": "G",   "avg_min": 28.0, "role": "starter", "depth": 1},
        "Isabelle Harrison":    {"pos": "F",   "avg_min": 22.0, "role": "starter", "depth": 1},
        "Stephanie Dolson":     {"pos": "C",   "avg_min": 16.0, "role": "bench",   "depth": 2},
        "Alanna Smith":         {"pos": "F",   "avg_min": 14.0, "role": "bench",   "depth": 2},
        "Dana Evans":           {"pos": "G",   "avg_min": 12.0, "role": "bench",   "depth": 2},
        "Elizabeth Williams":   {"pos": "C",   "avg_min":  8.0, "role": "bench",   "depth": 3},
    },
    "Minnesota Lynx": {
        "Napheesa Collier":     {"pos": "F",   "avg_min": 33.0, "role": "starter", "depth": 1},
        "Courtney Williams":    {"pos": "G",   "avg_min": 29.0, "role": "starter", "depth": 1},
        "Kayla McBride":        {"pos": "G",   "avg_min": 29.0, "role": "starter", "depth": 1},
        "Alanna Smith":         {"pos": "F",   "avg_min": 25.0, "role": "starter", "depth": 1},
        "Dorka Juhasz":         {"pos": "C",   "avg_min": 22.0, "role": "starter", "depth": 1},
        "Bridget Carleton":     {"pos": "G",   "avg_min": 17.0, "role": "bench",   "depth": 2},
        "Nikolina Milic":       {"pos": "F",   "avg_min": 14.0, "role": "bench",   "depth": 2},
        "Alissa Pili":          {"pos": "F",   "avg_min": 12.0, "role": "bench",   "depth": 2},
        "Diamond Miller":       {"pos": "G",   "avg_min": 10.0, "role": "bench",   "depth": 3},
    },
    "Los Angeles Sparks": {
        "Dearica Hamby":        {"pos": "F",   "avg_min": 30.0, "role": "starter", "depth": 1},
        "Rickea Jackson":       {"pos": "F",   "avg_min": 28.0, "role": "starter", "depth": 1},
        "Azura Stevens":        {"pos": "C",   "avg_min": 24.0, "role": "starter", "depth": 1},
        "Rae Burrell":          {"pos": "G",   "avg_min": 26.0, "role": "starter", "depth": 1},
        "Kia Nurse":            {"pos": "G",   "avg_min": 24.0, "role": "starter", "depth": 1},
        "Jasmine Thomas":       {"pos": "G",   "avg_min": 18.0, "role": "bench",   "depth": 2},
        "Katie Lou Samuelson":  {"pos": "G",   "avg_min": 15.0, "role": "bench",   "depth": 2},
        "Lexie Brown":          {"pos": "G",   "avg_min": 12.0, "role": "bench",   "depth": 3},
        "Li Yueru":             {"pos": "C",   "avg_min": 10.0, "role": "bench",   "depth": 3},
    },
    "Phoenix Mercury": {
        "Brittney Griner":      {"pos": "C",   "avg_min": 28.0, "role": "starter", "depth": 1},
        "Kahleah Copper":       {"pos": "G",   "avg_min": 30.0, "role": "starter", "depth": 1},
        "Sophie Cunningham":    {"pos": "G/F", "avg_min": 26.0, "role": "starter", "depth": 1},
        "Natasha Cloud":        {"pos": "G",   "avg_min": 24.0, "role": "starter", "depth": 1},
        "Rebecca Allen":        {"pos": "F",   "avg_min": 22.0, "role": "starter", "depth": 1},
        "Celeste Taylor":       {"pos": "G",   "avg_min": 16.0, "role": "bench",   "depth": 2},
        "Charisma Osborne":     {"pos": "G",   "avg_min": 13.0, "role": "bench",   "depth": 2},
        "Sug Sutton":           {"pos": "G",   "avg_min": 10.0, "role": "bench",   "depth": 3},
        "Diana Taurasi":        {"pos": "G",   "avg_min":  8.0, "role": "bench",   "depth": 3},
    },
    "Atlanta Dream": {
        "Rhyne Howard":         {"pos": "G",   "avg_min": 31.0, "role": "starter", "depth": 1},
        "Allisha Gray":         {"pos": "G",   "avg_min": 28.0, "role": "starter", "depth": 1},
        "Tina Charles":         {"pos": "C",   "avg_min": 26.0, "role": "starter", "depth": 1},
        "Naz Hillmon":          {"pos": "F",   "avg_min": 24.0, "role": "starter", "depth": 1},
        "Haley Jones":          {"pos": "G",   "avg_min": 23.0, "role": "starter", "depth": 1},
        "Jordin Canada":        {"pos": "G",   "avg_min": 17.0, "role": "bench",   "depth": 2},
        "Aerial Powers":        {"pos": "G",   "avg_min": 14.0, "role": "bench",   "depth": 2},
        "Kristy Wallace":       {"pos": "G",   "avg_min": 11.0, "role": "bench",   "depth": 3},
    },
    "Washington Mystics": {
        "Elena Delle Donne":    {"pos": "F",   "avg_min": 28.0, "role": "starter", "depth": 1},
        "Ariel Atkins":         {"pos": "G",   "avg_min": 27.0, "role": "starter", "depth": 1},
        "Shakira Austin":       {"pos": "C",   "avg_min": 26.0, "role": "starter", "depth": 1},
        "Brittney Sykes":       {"pos": "G",   "avg_min": 25.0, "role": "starter", "depth": 1},
        "Aaliyah Edwards":      {"pos": "F",   "avg_min": 22.0, "role": "starter", "depth": 1},
        "Natasha Cloud":        {"pos": "G",   "avg_min": 18.0, "role": "bench",   "depth": 2},
        "Stefanie Dolson":      {"pos": "C",   "avg_min": 14.0, "role": "bench",   "depth": 2},
        "Sug Sutton":           {"pos": "G",   "avg_min": 11.0, "role": "bench",   "depth": 3},
    },
    "Dallas Wings": {
        "Arike Ogunbowale":     {"pos": "G",   "avg_min": 33.0, "role": "starter", "depth": 1},
        "Satou Sabally":        {"pos": "F",   "avg_min": 30.0, "role": "starter", "depth": 1},
        "Teaira McCowan":       {"pos": "C",   "avg_min": 24.0, "role": "starter", "depth": 1},
        "Veronica Burton":      {"pos": "G",   "avg_min": 26.0, "role": "starter", "depth": 1},
        "Natasha Howard":       {"pos": "F",   "avg_min": 22.0, "role": "starter", "depth": 1},
        "Odyssey Sims":         {"pos": "G",   "avg_min": 17.0, "role": "bench",   "depth": 2},
        "Maddy Siegrist":       {"pos": "G/F", "avg_min": 14.0, "role": "bench",   "depth": 2},
        "Kalani Brown":         {"pos": "C",   "avg_min": 11.0, "role": "bench",   "depth": 3},
    },
    "Indiana Fever": {
        "Caitlin Clark":        {"pos": "G",   "avg_min": 33.0, "role": "starter", "depth": 1},
        "Aliyah Boston":        {"pos": "F",   "avg_min": 29.0, "role": "starter", "depth": 1},
        "Kelsey Mitchell":      {"pos": "G",   "avg_min": 28.0, "role": "starter", "depth": 1},
        "NaLyssa Smith":        {"pos": "F",   "avg_min": 25.0, "role": "starter", "depth": 1},
        "Lexie Hull":           {"pos": "G",   "avg_min": 22.0, "role": "starter", "depth": 1},
        "Erica Wheeler":        {"pos": "G",   "avg_min": 17.0, "role": "bench",   "depth": 2},
        "Damiris Dantas":       {"pos": "F",   "avg_min": 14.0, "role": "bench",   "depth": 2},
        "Kristy Wallace":       {"pos": "G",   "avg_min": 11.0, "role": "bench",   "depth": 3},
        "Destanni Henderson":   {"pos": "G",   "avg_min":  8.0, "role": "bench",   "depth": 3},
        "Sydney Colson":        {"pos": "G",   "avg_min":  6.0, "role": "bench",   "depth": 3},
    },
    "Golden State Valkyries": {
        "Tiffany Hayes":        {"pos": "G",   "avg_min": 28.0, "role": "starter", "depth": 1},
        "Kayla Thornton":       {"pos": "F",   "avg_min": 26.0, "role": "starter", "depth": 1},
        "Monique Billings":     {"pos": "F",   "avg_min": 24.0, "role": "starter", "depth": 1},
        "Carla Leite":          {"pos": "G",   "avg_min": 22.0, "role": "starter", "depth": 1},
        "Rayah Marshall":       {"pos": "C",   "avg_min": 20.0, "role": "starter", "depth": 1},
        "Laeticia Amihere":     {"pos": "F",   "avg_min": 18.0, "role": "bench",   "depth": 2},
        "Izzy Harrison":        {"pos": "F",   "avg_min": 15.0, "role": "bench",   "depth": 2},
        "Temi Fagbenle":        {"pos": "C",   "avg_min": 12.0, "role": "bench",   "depth": 2},
        "Lindsay Allen":        {"pos": "G",   "avg_min": 10.0, "role": "bench",   "depth": 3},
    },
}

# All known WNBA players across all teams for manual add
ALL_PLAYERS = sorted(set(
    player for roster in ROSTERS.values() for player in roster
))

# Position compatibility for replacement suggestions
POSITION_COMPAT = {
    "G":   ["G", "G/F"],
    "G/F": ["G", "G/F", "F"],
    "F":   ["F", "G/F", "F/C"],
    "F/C": ["F", "C", "F/C"],
    "C":   ["C", "F/C"],
}

POSITIONS = ["G", "G/F", "F", "F/C", "C"]

GAME_MINUTES = 200  # 5 players x 40 min
