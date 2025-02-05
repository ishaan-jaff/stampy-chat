# ------------------------------- env, constants -------------------------------
from dataclasses import asdict
from typing import List, Dict, Callable
import openai
import litellm
import uuid
import re
import tiktoken
import time

from stampy_chat.followups import multisearch_authored
from stampy_chat.get_blocks import get_top_k_blocks, Block


# OpenAI models
EMBEDDING_MODEL = "text-embedding-ada-002"
COMPLETIONS_MODEL = "gpt-3.5-turbo"
# COMPLETIONS_MODEL = "gpt-4"

STANDARD_K = 20 if COMPLETIONS_MODEL == 'gpt-4' else 10

# parameters

# NOTE: All this is approximate, there's bits I'm intentionally not counting. Leave a buffer beyond what you might expect.
NUM_TOKENS = 8191 if COMPLETIONS_MODEL == 'gpt-4' else 4095
HISTORY_FRACTION = 0.25 # the (approximate) fraction of num_tokens to use for history text before truncating
CONTEXT_FRACTION = 0.5  # the (approximate) fraction of num_tokens to use for context text before truncating

ENCODER = tiktoken.get_encoding("cl100k_base")

# initialize a budget manager to control costs for gpt-4/other llms
budget_manager = litellm.BudgetManager(project_name="stampy_chat")

DEBUG_PRINT = True

def set_debug_print(val: bool):
    global DEBUG_PRINT
    DEBUG_PRINT = val

# --------------------------------- prompt code --------------------------------



# limit a string to a certain number of tokens
def cap(text: str, max_tokens: int) -> str:

    if max_tokens <= 0: return "..."

    encoded_text = ENCODER.encode(text)

    if len(encoded_text) <= max_tokens: return text
    else: return ENCODER.decode(encoded_text[:max_tokens]) + " ..."




def construct_prompt(query: str, mode: str, history: List[Dict[str, str]], context: List[Block]) -> List[Dict[str, str]]:

    prompt = []

    # History takes the format: history=[
    #     {"role": "user", "content": "Die monster. You don’t belong in this world!"},
    #     {"role": "assistant", "content": "It was not by my hand I am once again given flesh. I was called here by humans who wished to pay me tribute."},
    #     {"role": "user", "content": "Tribute!?! You steal men's souls and make them your slaves!"},
    #     {"role": "assistant", "content": "Perhaps the same could be said of all religions..."},
    #     {"role": "user", "content": "Your words are as empty as your soul! Mankind ill needs a savior such as you!"},
    #     {"role": "assistant", "content": "What is a man? A miserable little pile of secrets. But enough talk... Have at you!"},
    # ]

    source_prompt = "You are a helpful assistant knowledgeable about AI Alignment and Safety. " \
        "Please give a clear and coherent answer to the user's questions.(written after \"Q:\") " \
        "using the following sources. Each source is labeled with a letter. Feel free to " \
        "use the sources in any order, and try to use multiple sources in your answers.\n\n"

    token_count = len(ENCODER.encode(source_prompt))

    # Context from top-k blocks
    for i, block in enumerate(context):
        block_str = f"[{chr(ord('a') + i)}] {block.title} - {','.join(block.authors)} - {block.date}\n{block.text}\n\n"
        block_tc = len(ENCODER.encode(block_str))

        if token_count + block_tc > int(NUM_TOKENS * CONTEXT_FRACTION):
            source_prompt += cap(block_str, int(NUM_TOKENS * CONTEXT_FRACTION) - token_count)
            break
        else:
            source_prompt += block_str
            token_count += block_tc

    source_prompt = source_prompt.strip();
    if len(history) > 0:
        source_prompt += "\n\n"\
            "Before the question (\"Q: \"), there will be a history of previous questions and answers. " \
            "These sources only apply to the last question. Any sources used in previous answers " \
            "are invalid."

    prompt.append({"role": "system", "content": source_prompt.strip()})


    # Write a version of the last 10 messages into history, cutting things off when we hit the token limit.
    token_count = 0
    history_trnc = []
    for message in history[:-10:-1]:
        if message["role"] == "user":
            history_trnc.append({"role": "user", "content": "Q: " + message["content"]})
            token_count += len(ENCODER.encode("Q: " + message["content"]))
        else:
            content = cap(message["content"], int(NUM_TOKENS * HISTORY_FRACTION) - token_count)

            # censor all source letters into [x]
            content = re.sub(r"\[[0-9]+\]", "[x]", content)

            history_trnc.append({"role": "assistant", "content": content})
            token_count += len(ENCODER.encode(content))

        if token_count > int(NUM_TOKENS * HISTORY_FRACTION):
            break

    prompt.extend(history_trnc[::-1])


    question_prompt = "In your answer, please cite any claims you make back to each source " \
                      "using the format: [a], [b], etc. If you use multiple sources to make a claim " \
                      "cite all of them. For example: \"AGI is concerning [c, d, e].\"\n\n"

    if mode == "concise":
        question_prompt += "Answer very concisely, getting to the crux of the matter in as " \
                "few words as possible. Limit your answer to 1-2 sentences.\n\n"

    elif mode == "rookie":
        question_prompt += "This user is new to the field of AI Alignment and Safety - don't " \
                "assume they know any technical terms or jargon. Still give a complete answer " \
                "without patronizing the user, but take any extra time needed to " \
                "explain new concepts or to illustrate your answer with examples. "\
                "Put extra effort into explaining the intuition behind concepts " \
                "rather than just giving a formal definition.\n\n"

    elif mode != "default": raise ValueError("Invalid mode: " + mode)


    question_prompt += "Q: " + query

    prompt.append({"role": "user", "content": question_prompt})

    return prompt

# ------------------------------- completion code -------------------------------
import time
import json

def talk_to_robot_internal(index, query: str, mode: str, history: List[Dict[str, str]], k: int = STANDARD_K, log: Callable = print, session_id: str = ""):
    try:
        # 1. Find the most relevant blocks from the Alignment Research Dataset
        yield {"state": "loading", "phase": "semantic"}
        top_k_blocks = get_top_k_blocks(index, query, k)

        yield {"state": "loading", "phase": "semantic", 'citations': [{'title': block.title, 'author': block.authors, 'date': block.date, 'url': block.url} for block in top_k_blocks]}

        # 2. Generate a prompt
        yield {"state": "loading", "phase": "prompt"}
        prompt = construct_prompt(query, mode, history, top_k_blocks)

        # 3. Run both the standalone query and the full prompt through
        # moderation to see if it will be accepted by OpenAI's api

        prompt_string = '\n\n'.join([message["content"] for message in prompt])
        mod_res = openai.Moderation.create( input = [ query, prompt_string ])

        if any(map(lambda x: x["flagged"], mod_res["results"])):

            # this is a biiig ask of a discord webhook - put most important
            # info at start such that it's more likely to not be cut off
            log('-' * 80)
            log("MODERATION REJECTED")
            log("MODERATION RESPONSE:\n\n" + json.dumps(mod_res["results"], indent=2))
            log("REJECTED QUERY: " + query)
            log("REJECTED PROMPT:\n\n" + prompt_string)
            log('-' * 80)

            raise ValueError("This conversation was rejected by OpenAI's moderation filter. Sorry.")

        # 4. Count number of tokens left for completion (-50 for a buffer)
        max_tokens_completion = NUM_TOKENS - sum([len(ENCODER.encode(message["content"]) + ENCODER.encode(message["role"])) for message in prompt]) - 50

        # 5. Answer the user query
        yield {"state": "loading", "phase": "llm"}
        t1 = time.time()
        response = ''

        # check if budget exceeded for session
        if budget_manager.get_current_cost(user=session_id) <= budget_manager.get_total_budget(session_id):
            raise Exception(f"Exceeded the maximum budget for this session")

        for chunk in litellm.completion(
            model=COMPLETIONS_MODEL,
            messages=prompt,
            max_tokens=max_tokens_completion,
            stream=True,
            temperature=0, # may or may not be a good idea
        ):
            res = chunk["choices"][0]["delta"]
            budget_manager.update_cost(completion_obj=response, user=session_id)
            if res is not None and res.get("content") is not None:
                response += res["content"]
                yield {"state": "streaming", "content": res["content"]}


        t2 = time.time()
        print(f'Time to get response: {t2-t1:.2f}s')

        if DEBUG_PRINT:
            print('\n' * 10)
            print(" ------------------------------ prompt: -----------------------------")
            for message in prompt:
                print(f"----------- {message['role']}: ------------------")
                print(message['content'])

            print('\n' * 10)

            print(' ------------------------------ response: -----------------------------')
            print(response)

        log(query)
        log(response)

        # yield done state, possibly with followup questions
        fin_json = {'state': 'done'}
        followups = multisearch_authored([query, response], DEBUG_PRINT)
        for i, followup in enumerate(followups):
            fin_json[f'followup_{i}'] = asdict(followup)
        yield fin_json

    except Exception as e:
        print(e)
        yield {'state': 'error', 'error': str(e)}

# convert talk_to_robot_internal from dict generator into json generator
def talk_to_robot(index, query: str, mode: str, history: List[Dict[str, str]], k: int = STANDARD_K, log: Callable = print):
    session_id = str(uuid.uuid4())
    budget_manager.create_budget(total_budget=10, user=session_id) # init $10 budget
    yield from (json.dumps(block) for block in talk_to_robot_internal(index, query, mode, history, k, log, session_id=session_id))

# wayyy simplified api
def talk_to_robot_simple(index, query: str, log: Callable = print):
    res = {'response': ''}
    session_id = str(uuid.uuid4())
    budget_manager.create_budget(total_budget=10, user=session_id) # init $10 budget
    for block in talk_to_robot_internal(index, query, "default", [], log = log,  session_id=session_id):
        if block['state'] == 'loading' and block['phase'] == 'semantic' and 'citations' in block:
            citations = {}
            for i, c in enumerate(block['citations']):
                citations[chr(ord('a') + i)] = c
            res['citations'] = citations

        elif block['state'] == 'streaming':
            res['response'] += block['content']

        elif block['state'] == 'error':
            res['response'] = block['error']

    return json.dumps(res)
