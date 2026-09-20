"""
Web search operation for SWE-bench.

Provides web search capabilities for finding relevant information.
"""

from typing import List, Any, Optional, Dict
from swarm.llm.format import Message
from swarm.graph import Node
from swarm.utils.log import logger
from swarm.environment.prompt.prompt_set_registry import PromptSetRegistry
from swarm.llm import LLMRegistry


class SWEWebSearch(Node):
    """
    Web search operation for SWE-bench.

    Uses web search to find relevant information about issues,
    APIs, or documentation.
    """

    def __init__(
        self,
        domain: str = 'swe_bench',
        model_name: Optional[str] = None,
        operation_description: str = "Search the web for relevant information.",
        max_token: int = 4096,
        max_search_results: int = 5,
        id=None,
    ):
        super().__init__(operation_description, id, True)
        self.domain = domain
        self.model_name = model_name
        self.llm = LLMRegistry.get(model_name)
        self.max_token = max_token
        self.max_search_results = max_search_results
        self.prompt_set = PromptSetRegistry.get(domain)
        self._search_engine = None

    @property
    def node_name(self):
        return self.__class__.__name__

    def _get_search_engine(self):
        """
        Get the appropriate search engine based on available API keys.

        Priority: Bing > SearchAPI > Google
        """
        if self._search_engine is not None:
            return self._search_engine

        import os
        from dotenv import load_dotenv
        load_dotenv()

        if os.getenv("BING_API_KEY"):
            from swarm.environment.tools.search.search import BingSearch
            self._search_engine = BingSearch()
        elif os.getenv("SEARCHAPI_API_KEY"):
            from swarm.environment.tools.search.search import SearchAPISearch
            self._search_engine = SearchAPISearch()
        elif os.getenv("GOOGLE_API_KEY"):
            from swarm.environment.tools.search.search import GoogleSearch
            self._search_engine = GoogleSearch()
        else:
            logger.warning("No search API key found. Web search will be simulated.")
            self._search_engine = None

        return self._search_engine

    def _perform_search(self, query: str, max_results: int = 5) -> List[Dict[str, str]]:
        """
        Perform a web search.

        Args:
            query: Search query
            max_results: Maximum number of results

        Returns:
            List of search results with title, url, and snippet
        """
        search_engine = self._get_search_engine()

        if search_engine is None:
            # Return simulated results if no search engine available
            return [{
                "title": "Search not available",
                "url": "",
                "snippet": "No search API key configured. Set BING_API_KEY, SEARCHAPI_API_KEY, or GOOGLE_API_KEY."
            }]

        try:
            results = search_engine.search(query, max_results=max_results)
            return results
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return [{
                "title": "Search error",
                "url": "",
                "snippet": f"Search failed: {e}"
            }]

    async def _execute(self, inputs: List[Any] = [], **kwargs):
        """Execute web search operation."""
        node_inputs = self.process_input(inputs)
        outputs = []

        for input in node_inputs:
            query = input.get("task", "")
            repo = input.get("repo", "")

            # Enhance query with repo information
            if repo:
                search_query = f"{repo} {query}"
            else:
                search_query = query

            # Perform search
            search_results = self._perform_search(
                search_query,
                max_results=self.max_search_results
            )

            # Format results
            results_text = "\n\n".join([
                f"Title: {r.get('title', '')}\nURL: {r.get('url', '')}\nSummary: {r.get('snippet', '')}"
                for r in search_results
            ])

            # Generate summary using LLM
            if search_results and search_results[0].get('url'):
                role = self.prompt_set.get_role() if self.prompt_set else "research assistant"
                constraint = self.prompt_set.get_constraint() if self.prompt_set else (
                    "Summarize the search results and their relevance to the issue."
                )

                prompt = f"""Based on the issue: {query}

Search results:
{results_text}

Provide a summary of the most relevant information for solving this issue.
"""

                message = [
                    Message(role="system", content=f"You are a {role}. {constraint}"),
                    Message(role="user", content=prompt)
                ]

                summary = await self.llm.agen(message, max_tokens=self.max_token)
            else:
                summary = "No relevant search results found."

            execution = {
                "operation": self.node_name,
                "query": query,
                "search_results": search_results,
                "summary": summary,
            }
            outputs.append(execution)
            self.memory.add(self.id, execution)

        return outputs
