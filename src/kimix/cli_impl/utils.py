def _get_slash_commands() -> set[str]:
    """Lazily get slash command names to avoid circular import at module level."""
    from .commands import _command_map_keys  # type: ignore[attr-defined]
    return _command_map_keys

def _input(text: str, text_arr: list[str], multi_line_mode: bool = False) -> str:
    if text_arr is None or len(text_arr) == 0:
        if text:
            print(text, end='', flush=True)
        return input()
    v = text_arr.pop(0)
    return v


def _split_text(lines: list[str], command_map: set[str] | None = None) -> list[str]:
    text_arr: list[str] = []
    current_text: list[str] = []
    for line in lines:
        strip_line = line.strip()
        if len(strip_line) == 0:
            current_text.append('')
            continue
        if strip_line.startswith('/'):
            if len(strip_line) > 1:
                cmd = strip_line[1:].split()[0]
                if command_map is not None and cmd not in command_map:
                    current_text.append(line)
                    continue
            if current_text:
                text_arr.append('\n'.join(current_text))
                current_text = []
            if len(strip_line) > 1:
                text_arr.append(strip_line)
        else:
            current_text.append(line)
    if current_text:
        text_arr.append('\n'.join(current_text))
    return text_arr
