# Codex
cd /Users/thierrycrouzet/Documents/python/WritingLog/
codex resume 01a08513-833e-7db1-9631-d5026ffa3f40

sh -c 'curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh'


# Web

./web

cd /Users/thierrycrouzet/Documents/python/WritingLog/site
python3 -m http.server 8001

http://localhost:8001



grep -A3 "Isa" site/data/size_evolution.json > extrait_isa.txt