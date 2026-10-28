---
name: negative-asyncio-script
tags: [v1.3, negative]
max_turns: 6
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
---

I wrote a plain Python asyncio script, no web framework, that downloads 200 URLs with aiohttp. It stalls
badly since I added time.sleep(1) between batches to be polite to the server. What is wrong and how should
I throttle it properly?
