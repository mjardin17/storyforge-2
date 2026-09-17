"""storyforge2/llm/__init__.py — shared LLM provider helpers.

Currently just Gemini text/JSON calls (storyforge2/llm/gemini_text.py),
factored out of research_agent.py's proven pattern so storyforge2/books/
trends.py doesn't duplicate it. Add other providers' shared plumbing here
as they show up, rather than re-copying request/retry/parsing logic into
each caller.
"""
