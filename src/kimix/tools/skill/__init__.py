from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field
from typing import override
from kimix.tools.skill.searching.file_builder import FileBuilder, formatted_print
from kimix.tools.common import _maybe_export_output_async
from kimix.utils import close_session_async, _create_session_async, prompt_async
from kimix.utils.system_prompt import SystemPromptType
from kimi_cli.session import Session


class IndexerParams(BaseModel):
    """Parameters for the indexer tool."""
    query: str = Field(
        description="Search keywords/query."
    )
    top_k: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of top results to return."
    )
    use_agent: bool = Field(
        default=False,
        description="If true, launch a sub-agent to analyze skill search results."
    )



class SkillSearch(CallableTool2[IndexerParams]):
    """Indexer tool for semantic search over text files."""

    name: str = "SkillSearch"
    description: str = "Search skills by keywords."
    params: type[IndexerParams] = IndexerParams
    file_builder_inited: bool = False
    file_builder: FileBuilder | None = None

    def __init__(self, session: Session):
        super().__init__()
        self._session = session

    @override
    async def __call__(self, params: IndexerParams) -> ToolReturnValue:
        if self._session.get_custom_data().get("is_sub_agent"):
            params.use_agent = False
        import kimix.base as base
        if not self.file_builder_inited:
            skill_dirs = base.get_skill_dirs(False)
            self.file_builder = FileBuilder(skill_dirs, '.kimix_cache/skill_config.json')
            self.file_builder_inited = True
        if self.file_builder is None:
            return ToolOk(output='')
        try:
            self.file_builder.update()
            results = self.file_builder.search(
                params.query, top_k=params.top_k)
            output = formatted_print(results)

            if not params.use_agent:
                return ToolOk(output=output)

            output_strs: list[str] = []

            def output_function(fn: str, is_thinking: bool) -> None:
                if fn and not is_thinking:
                    output_strs.append(fn)

            async def run_sub_agent(cancel_callable=None):
                session = None
                try:
                    session = await _create_session_async(
                        agent_file=base._default_agent_file_dir / "agent_skill_searcher.yaml",
                        is_sub_agent=True,
                        agent_type=SystemPromptType.SkillSearcher,
                    )
                    session.get_custom_data()['is_sub_agent'] = True
                    agent_prompt = (
                        f"Query: {params.query}\n\n"
                        f"Skill search results:\n{output}"
                    )
                    await prompt_async(
                        prompt_str=agent_prompt,
                        session=session,
                        output_function=output_function,
                        cancel_callable=cancel_callable,
                    )
                except Exception as e:
                    return str(e)
                finally:
                    if session:
                        await close_session_async(session)
                return None

            err_msg = await run_sub_agent()
            agent_output = await _maybe_export_output_async("".join(output_strs))
            if err_msg:
                return ToolError(output=agent_output, message=err_msg, brief="")
            return ToolOk(output=agent_output)
        except Exception as e:
            return ToolError(
                message=str(e),
                output="",
                brief="Search failed"
            )
