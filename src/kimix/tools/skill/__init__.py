import asyncio
from pathlib import Path

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field
from typing import override
from kimi_cli.session import Session

from kimix.base import MessageType, get_skill_dirs
from kimix.utils import close_session_async, _create_session_async
from kimix.utils.system_prompt import SystemPromptType, SystemPromptCallback


class IndexerParams(BaseModel):
    """Parameters for the indexer tool."""
    prompt: str = Field(
        description="Search prompt."
    )
    dest_path: str | list[str] | None = Field(
        default=None,
        description="Destination path."
    )


class Search(CallableTool2[IndexerParams]):
    """Indexer tool for semantic search over text files."""

    name: str = "Search"
    description: str = "Search skills by keywords."
    params: type[IndexerParams] = IndexerParams

    def __init__(self, session: Session):
        super().__init__()
        self._session = session
        self._semaphore = asyncio.Semaphore(8)

    @override
    async def __call__(self, params: IndexerParams) -> ToolReturnValue:
        if self._session is not None and self._session.custom_config.get("is_sub_agent"):
            return ToolError(
                output='',
                message='Recursive sub-agent call detected',
                brief='sub-agent recursively'
            )
        async with self._semaphore:
            try:
                output_strs = []

                def output_function(fn: str, msg_type: MessageType) -> None:
                    if fn and msg_type != MessageType.Thinking:
                        output_strs.append(fn)

                async def prompt_async(cancel_callable=None):
                    session = None
                    err_msg_inner = None
                    try:
                        import kimix.base as base
                        custom_config = self._session.custom_config
                        chat_provider = custom_config.get("chat_provider")
                        default_sub_provider = base._default_sub_provider if base._default_sub_provider is not None else custom_config.get("provider_dict", base._default_provider)
                        dest_paths = [params.dest_path] if isinstance(params.dest_path, str) else params.dest_path
                        if dest_paths is not None:
                            valid_paths = []
                            for dp in dest_paths:
                                if not dp or not dp.strip():
                                    continue
                                p = Path(dp)
                                if not p.exists():
                                    continue
                                if not p.is_dir():
                                    continue
                                try:
                                    if not any(p.iterdir()):
                                        continue
                                except PermissionError:
                                    pass
                                valid_paths.append(dp)
                            dest_path = valid_paths
                        else:
                            skill_dirs = [str(d) for d in get_skill_dirs(use_kaos_path=False)]
                            cache_dir = Path('.kimix_cache/')
                            cache_path = str(cache_dir) if cache_dir.exists() else None
                            if skill_dirs and cache_path:
                                dest_path = skill_dirs + [cache_path]
                            elif skill_dirs:
                                dest_path = skill_dirs
                            elif cache_path:
                                dest_path = [cache_path]
                            else:
                                dest_path = []
                        
                        if not dest_path:
                            err_msg_inner = "No valid destination paths found."
                        else:
                            dest_path_str = ', '.join(dest_path)
                            session = await _create_session_async(
                                agent_file=base._default_agent_file_dir / 'agent_searcher.json',
                                agent_type=SystemPromptType.SkillSearcher,
                                provider_dict=default_sub_provider,
                                chat_provider=chat_provider,
                                thinking=False,
                                anonymous=True,
                                max_ralph_iterations=0
                            )
                            sub_custom_config = session.get_custom_config()
                            if sub_custom_config is not None:
                                sub_custom_config['is_sub_agent'] = True
                            import kimix.utils as utils
                            prompt = f'Search:\n```\n{params.prompt}\n```in `{dest_path_str}`'
                            await utils.prompt_async(prompt_str=prompt, session=session, output_function=output_function, info_print=False, cancel_callable=cancel_callable, merge_wire_messages=True)
                    except Exception as e:
                        err_msg_inner = str(e)
                    finally:
                        if session:
                            try:
                                await close_session_async(session)
                            except Exception:
                                pass
                    return err_msg_inner

                err_msg = await prompt_async()
                if err_msg:
                    return ToolError(output='\n'.join(output_strs), message=err_msg, brief=f'skill search task failed: {params.prompt}')
                output = '\n'.join(output_strs)
                return ToolOk(output=output, brief=params.prompt)
            except Exception as exc:
                return ToolError(
                    output="",
                    message=str(exc),
                    brief=f"Failed to create session: {params.prompt}",
                )
