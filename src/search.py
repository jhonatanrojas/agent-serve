from duckduckgo_search import DDGS


def web_search(query: str, max_results: int = 5) -> str:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return "Sin resultados"
        return "\n\n".join(
            f"**{r['title']}**\n{r['body']}\n{r['href']}" for r in results
        )
    except Exception as e:
        return f"Error en búsqueda: {e}"
