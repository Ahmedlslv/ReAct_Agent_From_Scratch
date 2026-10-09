"""
5-step multi-hop QA task for the ReAct agent.

This task requires chaining 3+ tools to answer:
    1. file_read  — get the number of lines from notes.txt
    2. calc       — compute sqrt of that number
    3. search     — look up what ReAct stands for
    4. calc       — do a follow-up computation
    5. Answer     — synthesize everything

Run:
    python run_task.py
"""

from agent import run_react  # agent/__init__.py re-exports run_react from agent.base

TASK = (
    "I have a file called data/notes.txt. "
    "First, read it and tell me how many lines it has. "
    "Then compute the square root of that number. "
    "Then search for what 'ReAct' stands for in the context of LLM agents. "
    "Finally, multiply the square root result by 3 and give me the final answer "
    "along with what ReAct stands for."
)

TASK2 = (
    "I have a file called data/notes.txt. "
    "First, read it and tell me who is the author. "
    "Then search that author and tell me what is his job title. "
    "Finally, square root his first name length"
)

TASK3 = (
    "I have a file called data/notes.txt. "
    "First, read it and tell me the date of the project. "
    "Then search if something intresting happened on that date in the last 10 years. "
    "Then, divide that day by 2, then multiply it by the number of lines in the file "
)

if __name__ == "__main__":
    run_react(TASK, max_steps=10, verbose=True)
