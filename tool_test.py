"""Tool-choice test for the music café agent (report part 7.2).

For every question it is written down which tool(s) are accepted. The test asks the real agent,
looks at the tool trace, and counts a question as OK if at least one accepted tool was used.
An empty set {} means: "the agent must NOT use any tool" (for questions that are not our job).
"""
import importlib.util
import time
from pathlib import Path

HERE = Path(__file__).parent

# Load app-gradio.py. The file name has a hyphen, so a normal "import" does not work.
spec = importlib.util.spec_from_file_location("cafe_app", HERE / "app-gradio.py")
cafe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cafe)          # builds the database; the web page is NOT started
run_agent = cafe.run_agent

SECONDS_BETWEEN = 6    # a free plan allows only a few calls per minute (error 429)

# (question, tools that are accepted)
tool_test_set = [
    ("What time do you open?",                              {"get_cafe_info"}),
    ("Where is the café?",                                  {"get_cafe_info"}),
    ("Can I pay by credit card?",                           {"get_cafe_info"}),
    ("Do you deliver?",                                     {"get_cafe_info"}),
    ("Is there parking?",                                   {"get_cafe_info"}),
    ("How does the song wish work?",                        {"get_cafe_info"}),
    ("Which songs do you have by Teresa Teng?",             {"search_songs"}),
    ("有五月天的勇敢嗎？",                                       {"search_songs"}),
    ("What drinks do you have?",                            {"search_drinks"}),
    ("How much is a coffee?",                               {"get_price", "search_drinks"}),
    ("Do you have iced milk tea?",                          {"get_price", "search_drinks"}),
    ("I would like two coffees and one iced milk tea. How much is that?", {"calculate_total"}),
    ("請問你們幾點開門？",                                       {"get_cafe_info"}),
    ("Can you play Bohemian Rhapsody by Queen?",            {"transfer_to_human"}),   # not on our list
    ("I want to buy a record.",                             {"transfer_to_human"}),
    ("I want to speak to the manager.",                     {"transfer_to_human"}),
    ("What is the weather in Kaohsiung tomorrow?",          set()),                   # not our job: no tool
]


def ask(question):
    """One question in a fresh conversation. Returns the list of tools used, or None if the API failed.
    On error 429 (too many calls) it waits and tries again."""
    for attempt in range(3):
        try:
            _, trace = run_agent([{"role": "user", "content": question}])
            return [step["tool"] for step in trace]
        except Exception as error:
            if "429" in str(error) and attempt < 2:
                print("   (429: too many calls, waiting 40 seconds)")
                time.sleep(40)
            else:
                print(f"   API ERROR: {error}")
                return None
    return None


def tool_choice_test():
    correct = 0
    for question, accepted in tool_test_set:
        used = ask(question)
        if used is None:
            ok = False
        elif accepted:
            ok = bool(accepted & set(used))        # at least one accepted tool was used
        else:
            ok = used == []                        # no tool was expected
        correct += ok
        print(f"{'OK  ' if ok else 'MISS'} {question[:44]:44s} accepted {'/'.join(sorted(accepted)) or 'no tool':24s} used {used}")
        time.sleep(SECONDS_BETWEEN)
    print(f"\ntool choice = {correct}/{len(tool_test_set)} = {correct / len(tool_test_set):.0%}")


def queue_size():
    return cafe.query("SELECT COUNT(*) AS n FROM queue WHERE status = 'waiting'")[0]["n"]


def song_wish_test():
    """A song wish needs two turns: the agent must ask first, and write to the queue only after 'yes'."""
    print("\nSong wish test (confirmation step):")
    before = queue_size()
    chat = [{"role": "user", "content": "I would like to hear 在水一方, please."}]
    answer, trace = run_agent(chat)
    after_first = queue_size()
    print(f"  turn 1: tools {[s['tool'] for s in trace]}, queue {before} -> {after_first}")
    time.sleep(SECONDS_BETWEEN)

    chat += [{"role": "assistant", "content": answer},
             {"role": "user", "content": "Yes, please put it in the queue."}]
    _, trace = run_agent(chat)
    after_second = queue_size()
    print(f"  turn 2: tools {[s['tool'] for s in trace]}, queue {after_first} -> {after_second}")

    ok = after_first == before and after_second == before + 1
    print(f"  {'OK  ' if ok else 'MISS'} nothing written before 'yes', one song written after 'yes'")


if __name__ == "__main__":
    tool_choice_test()
    song_wish_test()
