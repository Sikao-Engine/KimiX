import asyncio
from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field
from kimi_cli.session import Session
from kimix.base import MessageType
from kimix.utils import close_session_async, _create_session_async
from kimix.utils.system_prompt import SystemPromptType


class SubAgentParams(BaseModel):
    prompt: str = Field(
        description="Task instructions for the sub-agent."
    )
    session_id: str | None = Field(
        default=None,
        description="Optional session ID to resume an existing sub-agent session."
    )


class Agent(CallableTool2):
    name: str = "Agent"
    description: str = "Launch a sub-agent for a task."
    params: type[SubAgentParams] = SubAgentParams

    def __init__(self, session: Session):
        super().__init__()
        self._session = session
        self._semaphore = asyncio.Semaphore(8)

    async def __call__(self, params: SubAgentParams) -> ToolReturnValue:
        if self._session is not None and self._session.custom_config.get("is_sub_agent"):
            return ToolError(
                output='',
                message='Recursive sub-agent call detected',
                brief='sub-agent recursively'
            )
        async with self._semaphore:
            try:
                output_strs = []
                sub_session_id: str | None = None

                def output_function(fn: str, msg_type: MessageType) -> None:
                    if fn and msg_type != MessageType.Thinking:
                        output_strs.append(fn)

                async def prompt_async(cancel_callable=None):
                    nonlocal sub_session_id
                    session = None
                    err_msg_inner = None
                    try:
                        import kimix.base as base
                        custom_config = self._session.custom_config
                        chat_provider = custom_config.get("chat_provider")
                        default_sub_provider = base._default_sub_provider if base._default_sub_provider is not None else custom_config.get("provider_dict", base._default_provider)
                        resume = params.session_id is not None
                        session = await _create_session_async(
                            session_id=params.session_id,
                            agent_file=base._default_agent_file_dir / 'agent_subagent.json', agent_type=SystemPromptType.TrivialSubAgent,
                            provider_dict=default_sub_provider,
                            chat_provider=chat_provider,
                            resume=resume,
                            anonymous=not resume,
                            max_ralph_iterations=0)
                        sub_session_id = session.id
                        sub_custom_config = session.get_custom_config()
                        if sub_custom_config is not None:
                            sub_custom_config['is_sub_agent'] = True
                        import kimix.utils as utils
                        await utils.prompt_async(prompt_str=params.prompt, session=session, output_function=output_function, info_print=False, cancel_callable=cancel_callable, merge_wire_messages=True)
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
                output = '\n'.join(output_strs)
                if err_msg:
                    if sub_session_id is not None:
                        output = f"Session ID: {sub_session_id}\n\n{output}"
                    return ToolError(output=output, message=err_msg, brief=f'sub-agent task failed: {params.prompt}')
                if sub_session_id is not None:
                    output = f"Session ID: {sub_session_id}\n\n{output}"
                return ToolOk(output=output, brief=params.prompt)
            except Exception as exc:
                return ToolError(
                    output="",
                    message=str(exc),
                    brief=f"Failed to create session: {params.prompt}",
                )
