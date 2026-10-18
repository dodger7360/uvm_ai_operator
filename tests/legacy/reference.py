"""Load a pinned historical module for explicit offline comparison tests only."""
from functools import lru_cache
import subprocess
from types import ModuleType

@lru_cache(maxsize=None)
def telegram_bot():
    runtime_source=subprocess.check_output(['git','show',
        'aa11380dfe1ed646fec2355b22880fdb03b0d706:steel_agent_v2/runtime.py']).decode('utf-8')
    runtime=ModuleType('historical_runtime_aa11380')
    exec(compile(runtime_source,'historical_runtime_aa11380.py','exec'),runtime.__dict__)
    source=subprocess.check_output(['git','show',
        'aa11380dfe1ed646fec2355b22880fdb03b0d706:telegram_bot.py']).decode('utf-8')
    # Freeze that historical dependency too. Never patch the current v2 selector.
    source=source.replace('from steel_agent_v2.runtime import selection, connect',
                          'selection, connect = _historical_runtime.selection, _historical_runtime.connect')
    module=ModuleType('historical_telegram_bot_aa11380')
    module._historical_runtime=runtime
    exec(compile(source,'historical_telegram_bot_aa11380.py','exec'),module.__dict__)
    return module
