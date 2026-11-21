"""The product catalog and the facts sheet, read-only (both are edited as YAML files)."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from salesfinder.industries import INDUSTRIES
from salesfinder.web.deps import AuthDep, CatalogDep, FactsDep
from salesfinder.web.templating import render

router = APIRouter()


@router.get("/catalog", response_class=HTMLResponse)
async def catalog_page(
    request: Request, auth: AuthDep, catalog: CatalogDep, facts: FactsDep
) -> HTMLResponse:
    context = {
        "auth": auth,
        "products": catalog.products,
        "facts": facts,
        "industries": INDUSTRIES,
    }
    return render(request, "catalog.html", context)
