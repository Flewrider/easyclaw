#!/bin/bash
# clawdy-memory-cleanup.sh — daily memory pruning
# Deletes low-importance stale memories to keep the db lean.
# Rules:
#   - importance < 5 and older than 30 days  → delete
#   - importance < 8 and older than 90 days  → delete
#   - sessions older than 90 days            → delete
#   - high-importance (>= 8) memories        → always kept

DB="$HOME/.easyclaw/memories.db"
HOME_SLUG="${HOME//\//-}"
MEMORY_MD="$HOME/.claude/projects/${HOME_SLUG}/memory/MEMORY.md"
LOG="$HOME/.easyclaw/activity-log.md"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M')

if [ ! -f "$DB" ]; then
  echo "[$TIMESTAMP] memory-cleanup: db not found, skipping" >> "$LOG"
  exit 0
fi

deleted=$(python3 - <<'PYEOF'
import sqlite3, datetime, sys

db_path = __import__('os').path.expanduser("~/.easyclaw/memories.db")
conn = sqlite3.connect(db_path)
c = conn.cursor()

now = datetime.datetime.utcnow()

# Memories with importance < 5 older than 30 days
cutoff_30 = (now - datetime.timedelta(days=30)).isoformat(sep=' ', timespec='seconds')
c.execute("""
    DELETE FROM memories
    WHERE importance < 5 AND updated_at < ?
""", (cutoff_30,))
d1 = c.rowcount

# Memories with importance < 8 older than 90 days
cutoff_90 = (now - datetime.timedelta(days=90)).isoformat(sep=' ', timespec='seconds')
c.execute("""
    DELETE FROM memories
    WHERE importance < 8 AND updated_at < ?
""", (cutoff_90,))
d2 = c.rowcount

# Sessions older than 90 days
c.execute("""
    DELETE FROM sessions
    WHERE started_at < ?
""", (cutoff_90,))
d3 = c.rowcount

conn.commit()

# Rebuild FTS index
c.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
conn.commit()
conn.close()

print(d1 + d2)
PYEOF
)

echo "[$TIMESTAMP] memory-cleanup: removed ${deleted} stale memories" >> "$LOG"

# Regenerate MEMORY.md index
MEMORY_DIR="$HOME/.claude/projects/${HOME_SLUG}/memory"
mkdir -p "$MEMORY_DIR"

python3 - <<PYEOF
import sqlite3, datetime, os

db_path = os.path.expanduser("~/.easyclaw/memories.db")
import pathlib
_home = pathlib.Path.home()
_slug = str(_home).replace("/", "-")
memory_md = str(_home / ".claude" / "projects" / _slug / "memory" / "MEMORY.md")

conn = sqlite3.connect(db_path)
c = conn.cursor()

now = datetime.datetime.now()
month_ago = (now - datetime.timedelta(days=30)).isoformat(sep=' ', timespec='seconds')

c.execute("SELECT COUNT(*) FROM memories")
total = c.fetchone()[0]
c.execute("SELECT COUNT(*) FROM memories WHERE created_at >= ?", (month_ago,))
this_month = c.fetchone()[0]

c.execute("""
    SELECT category, COUNT(*), MAX(date(updated_at)) as last
    FROM memories GROUP BY category ORDER BY last DESC
""")
categories = c.fetchall()

c.execute("""
    SELECT id, category, title FROM memories
    WHERE importance >= 8 ORDER BY importance DESC, updated_at DESC
""")
pinned = c.fetchall()

conn.close()

lines = [
    "# Clawdy Memory System",
    f"*{now.strftime('%Y-%m-%d %H:%M')} | {total} total memories | {this_month} this month*",
    "",
    "## How to use",
    "Memory content is NOT stored here to keep context lean.",
    "Fetch memories on demand with:",
    "- \`clawdy-memory search <query>\` — full-text search across all memories",
    "- \`clawdy-memory show <id>\` — get full content by ID",
    "- \`clawdy-memory list --days 7\` — recent entries",
    "- \`clawdy-memory add <category> <title> <content>\` — save new memory",
    "",
    "## Memory Index",
    "| Category | Count | Last updated |",
    "|----------|-------|--------------|",
]
for cat, cnt, last in categories:
    lines.append(f"| {cat} | {cnt} | {last} |")

lines += [
    "",
    "## Pinned (importance ≥ 8) — titles only, use \`show <id>\` for content",
]
for mid, cat, title in pinned:
    lines.append(f"- [{mid}] \`{cat}\` **{title}**")
lines.append("")

with open(memory_md, "w") as f:
    f.write("\n".join(lines))

print(f"MEMORY.md updated — {total} memories, {len(pinned)} pinned")
PYEOF
