from kimix.utils import *
prompt('''
according to python files under `src/kimix/utils`,
update `.agents/skills/api/SKILL.md`
''', session = create_session(), close_session_after_prompt=True)

prompt('''
read scripts in src/kimix/cli_impl/, update docs/tutorials/1_quick_start.md
''')

prompt('''
read scripts under src/kimix/cli_impl/, update docs/tutorials/1_quick_start.md
''')

prompt('''
read src/kimix/utils/prompt.py, update docs/tutorials/2_long_task.md
''')

prompt('''
read src/kimix/agent_worker.json , update docs/tutorials/3_builtin_tools.md
''')

prompt('''
read scripts under src/kimix/memory/ , update docs/tutorials/6_memory.md
''')