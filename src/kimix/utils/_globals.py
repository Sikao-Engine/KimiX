from typing import Any
from collections import OrderedDict
from kimi_agent_sdk import Session
import threading

TextSearchIndex: Any = None
SearchResult: Any = None

_default_session: Session | None = None
_default_role: Any = None

_should_print_usage = threading.local()
_should_print_usage.value = True
