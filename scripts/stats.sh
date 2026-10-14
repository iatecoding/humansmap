#!/bin/bash
# Usage: ./stats.sh [duration]
# Examples: ./stats.sh 30m, ./stats.sh 2h, ./stats.sh 24h
# Default: 60m

SINCE="${1:-60m}"

docker compose logs --since="$SINCE" caddy 2>/dev/null | python3 -c "
import sys, json
from collections import Counter

humans, bot_count, pages, countries, searches = set(), 0, Counter(), Counter(), []

for line in sys.stdin:
    try:
        log = json.loads(line.strip().split('caddy-1  | ', 1)[-1])
        req = log.get('request', {})
        headers = req.get('headers', {})
        ua = (headers.get('User-Agent') or [''])[0]
        ip = req.get('client_ip', '')
        uri = req.get('uri', '')
        country = (headers.get('Cf-Ipcountry') or ['??'])[0]

        if any(b in ua.lower() for b in ['bot','crawler','spider','facebook','ahref','semrush','yandex','meta-web','bytespider','gptbot','claudebot']):
            bot_count += 1
            continue

        if ip:
            humans.add(ip)
            countries[country] += 1

        if '/person/' in uri:
            pages[uri] += 1
        elif '/api/search' in uri:
            q = ''
            for part in uri.split('?')[-1].split('&'):
                if part.startswith('q='):
                    q = part[2:].replace('+', ' ').replace('%20', ' ')
            if q:
                searches.append(q)
            pages[uri] += 1
        elif uri == '/':
            pages[uri] += 1
    except:
        pass

print(f'  Traffic summary (last $SINCE)')
print(f'  {\"=\" * 40}')
print(f'  Unique human IPs:  {len(humans)}')
print(f'  Bot requests:      {bot_count}')
print()

if countries:
    print(f'  Top countries:')
    for c, n in countries.most_common(10):
        print(f'    {c}  {n}')
    print()

if searches:
    print(f'  Searches:')
    for s in searches:
        print(f'    \"{s}\"')
    print()

if pages:
    print(f'  Top 20 pages:')
    for page, count in pages.most_common(20):
        label = page.split('/')[-1].split('?')[0] if '/person/' in page else page
        print(f'    {count:>4}x  {label}')
"
