from fastapi import FastAPI, APIRouter, HTTPException, Query
from fastapi.responses import Response
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import re
import logging
import httpx
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import List
import uuid
from datetime import datetime, timezone


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Create the main app without a prefix
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")


# Define Models
class StatusCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")  # Ignore MongoDB's _id field
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class StatusCheckCreate(BaseModel):
    client_name: str

# Add your routes to the router instead of directly to app
@api_router.get("/")
async def root():
    return {"message": "Hello World"}

@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    status_dict = input.model_dump()
    status_obj = StatusCheck(**status_dict)
    
    # Convert to dict and serialize datetime to ISO string for MongoDB
    doc = status_obj.model_dump()
    doc['timestamp'] = doc['timestamp'].isoformat()
    
    _ = await db.status_checks.insert_one(doc)
    return status_obj

@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    # Exclude MongoDB's _id field from the query results
    status_checks = await db.status_checks.find({}, {"_id": 0}).to_list(1000)
    
    # Convert ISO string timestamps back to datetime objects
    for check in status_checks:
        if isinstance(check['timestamp'], str):
            check['timestamp'] = datetime.fromisoformat(check['timestamp'])
    
    return status_checks


# Yahoo Finance proxy — mirrors the Netlify Function so the preview URL works end-to-end.
_YF_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-^=]{1,20}$", re.IGNORECASE)

# Cache the Yahoo crumb (lasts ~30 min) so we don't handshake every request.
_CRUMB_CACHE = {"crumb": None, "cookies": None, "ts": 0}


async def _get_crumb():
    import time
    if _CRUMB_CACHE["crumb"] and time.time() - _CRUMB_CACHE["ts"] < 1500:
        return _CRUMB_CACHE["crumb"], _CRUMB_CACHE["cookies"]
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    import urllib.parse
    # Try direct first, fall back to allorigins for cookie/crumb fetch
    for direct in (True, False):
        try:
            async with httpx.AsyncClient(timeout=14, follow_redirects=True,
                                         headers={"User-Agent": ua}) as c:
                if direct:
                    await c.get("https://fc.yahoo.com")
                    r = await c.get("https://query2.finance.yahoo.com/v1/test/getcrumb")
                else:
                    await c.get("https://api.allorigins.win/raw?url="
                                + urllib.parse.quote("https://fc.yahoo.com", safe=""))
                    r = await c.get("https://api.allorigins.win/raw?url="
                                    + urllib.parse.quote("https://query2.finance.yahoo.com/v1/test/getcrumb", safe=""))
                crumb = r.text.strip() if r.status_code == 200 else None
                if crumb and len(crumb) < 30 and not crumb.startswith("{"):
                    _CRUMB_CACHE["crumb"] = crumb
                    _CRUMB_CACHE["cookies"] = dict(c.cookies)
                    _CRUMB_CACHE["ts"] = time.time()
                    return crumb, dict(c.cookies)
        except Exception:
            pass
    return None, {}


async def _try_fetch(url: str, headers: dict, timeout: float):
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        r = await client.get(url, headers=headers)
        if r.status_code == 200 and r.content and b'"chart"' in r.content[:64]:
            return r.content
        raise RuntimeError(f"bad response {r.status_code}")


@api_router.get("/yahoo")
async def yahoo_proxy(
    symbol: str = Query(...),
    range: str = Query("1y"),
    interval: str = Query("1d"),
):
    if not _YF_SYMBOL_RE.match(symbol):
        raise HTTPException(status_code=400, detail="Invalid symbol")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval={interval}&range={range}&includePrePost=false"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    import urllib.parse
    allorigins = "https://api.allorigins.win/raw?url=" + urllib.parse.quote(url, safe="")
    # Strategy chain — try each, fall through on failure.
    # Allorigins first since our pod IP is often rate-limited by Yahoo directly;
    # on Netlify's edge IPs the order can be flipped (direct first), but allorigins
    # gives a consistent path that works from any IP.
    strategies = [
        (allorigins, {}, 18.0),      # public proxy (different IP)
        (url, headers, 8.0),         # direct Yahoo
        (allorigins, {}, 18.0),      # retry public proxy
        (url, headers, 8.0),         # retry direct Yahoo
    ]
    last_err = None
    for u, h, t in strategies:
        try:
            content = await _try_fetch(u, h, t)
            return Response(
                content=content,
                status_code=200,
                media_type="application/json",
                headers={"Cache-Control": "public, max-age=60"},
            )
        except Exception as e:
            last_err = e
    raise HTTPException(status_code=502, detail=f"All fetch strategies failed: {last_err}")


@api_router.get("/yahoo/search")
async def yahoo_search(q: str = Query(..., min_length=1, max_length=40)):
    """Search any NSE/BSE/global stock by name or ticker."""
    import urllib.parse
    target = (
        "https://query2.finance.yahoo.com/v1/finance/search"
        f"?q={urllib.parse.quote(q)}&lang=en-US&region=IN&quotesCount=12&newsCount=0"
    )
    proxy = "https://api.allorigins.win/raw?url=" + urllib.parse.quote(target, safe="")
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    for url, h, t in [(target, {"User-Agent": ua}, 8.0),
                       (proxy, {}, 15.0),
                       (target, {"User-Agent": ua}, 8.0)]:
        try:
            async with httpx.AsyncClient(timeout=t, follow_redirects=True) as c:
                r = await c.get(url, headers=h)
            if r.status_code == 200 and b'"quotes"' in r.content[:128]:
                return Response(content=r.content, media_type="application/json",
                                headers={"Cache-Control": "public, max-age=300"})
        except Exception:
            pass
    raise HTTPException(status_code=502, detail="search failed")


@api_router.get("/yahoo/quote")
async def yahoo_quote(symbol: str = Query(...)):
    """Live fundamentals — P/E, EPS, ROE, market cap, margins, etc."""
    if not _YF_SYMBOL_RE.match(symbol):
        raise HTTPException(status_code=400, detail="Invalid symbol")
    crumb, cookies = await _get_crumb()
    if not crumb:
        raise HTTPException(status_code=502, detail="Could not get Yahoo crumb")
    modules = ("summaryDetail,defaultKeyStatistics,financialData,assetProfile,price,"
               "earnings,incomeStatementHistory,balanceSheetHistory")
    base = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
            f"?modules={modules}&crumb={crumb}")
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    import urllib.parse
    fallback = "https://api.allorigins.win/raw?url=" + urllib.parse.quote(base, safe="")
    last_err = None
    for url in (base, fallback, base, fallback):
        try:
            async with httpx.AsyncClient(timeout=16, follow_redirects=True,
                                         headers={"User-Agent": ua}, cookies=cookies) as c:
                r = await c.get(url)
            if r.status_code == 200 and b'"quoteSummary"' in r.content[:64]:
                return Response(content=r.content, media_type="application/json",
                                headers={"Cache-Control": "public, max-age=120"})
            if r.status_code == 401:
                _CRUMB_CACHE["crumb"] = None
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
    raise HTTPException(status_code=502, detail=f"quote failed: {last_err}")

# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()