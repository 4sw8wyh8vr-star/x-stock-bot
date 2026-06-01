"""
Poll an X (Twitter) user's timeline via direct HTTP calls to X's internal GraphQL API.
Uses browser-exported cookies — no API key, no twikit, no payment required.
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx

from config import Config

logger = logging.getLogger(__name__)

COOKIES_FILE = os.path.join(os.path.dirname(__file__), "x_cookies.json")

# X's internal web client bearer token (public, same for all web users)
BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# GraphQL endpoint — use x.com (not twitter.com); query IDs from twikit 1.7.6
# which confirmed 200 OK with these IDs + cookies approach
URL_USER_BY_SCREEN_NAME = (
    "https://x.com/i/api/graphql/"
    "NimuplG1OB7Fd2btCLdBOw/UserByScreenName"
)
URL_USER_TWEETS = (
    "https://x.com/i/api/graphql/"
    "HuTx74BxAnezK1gWvYY7zg/UserTweets"
)

# Feature flags exactly as twikit 1.7.6 sends for UserByScreenName
FEATURES_USER = json.dumps({
    "hidden_profile_likes_enabled": True,
    "hidden_profile_subscriptions_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
})

# fieldToggles required by UserByScreenName (twikit 1.7.6 sends this)
FIELD_TOGGLES_USER = json.dumps({"withAuxiliaryUserLabels": False})

# Feature flags for UserTweets
FEATURES_TWEETS = json.dumps({
    "rweb_lists_timeline_redesign_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "tweetypie_unmention_optimization_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": False,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": False,
    "responsive_web_enhance_cards_enabled": False,
})


@dataclass
class Tweet:
    id: str
    text: str
    author: str
    created_at: str
    url: str


def _find_nested(obj, key):
    """Recursively search a nested dict/list for all values of a given key."""
    results = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                results.append(v)
            results.extend(_find_nested(v, key))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(_find_nested(item, key))
    return results


class XMonitor:
    def __init__(self, config: Config, username: Optional[str] = None):
        self.config = config
        self.username = username or config.X_USERNAME   # allow per-account override
        self._cookies: dict = {}
        self._ct0: str = ""
        self._user_id: Optional[str] = None
        self._ready = False

    async def initialize(self) -> None:
        # Prefer env var (for cloud deployment), fall back to local file
        cookies_json = os.environ.get("X_COOKIES_JSON")
        if cookies_json:
            raw = json.loads(cookies_json)
            logger.info("Cookies loaded from X_COOKIES_JSON env var ✓")
        elif os.path.exists(COOKIES_FILE):
            with open(COOKIES_FILE) as f:
                raw = json.load(f)
            logger.info("Cookies loaded from x_cookies.json file ✓")
        else:
            raise FileNotFoundError(
                f"No cookies found. Set X_COOKIES_JSON env var or create {COOKIES_FILE}"
            )

        # Cookie-Editor exports a list; we need {name: value}
        if isinstance(raw, list):
            self._cookies = {c["name"]: c["value"] for c in raw}
        else:
            self._cookies = raw

        self._ct0 = self._cookies.get("ct0", "")
        self._ready = True
        logger.info("Cookies loaded ✓  (ct0 present: %s)", bool(self._ct0))

    def _headers(self) -> dict:
        return {
            "authorization": f"Bearer {BEARER}",
            "x-csrf-token": self._ct0,
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "content-type": "application/json",
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "referer": "https://x.com/",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }

    async def _get_user_id(self, client: httpx.AsyncClient) -> str:
        if self._user_id:
            return self._user_id

        variables = json.dumps({
            "screen_name": self.username,
            "withSafetyModeUserFields": True,
        })
        params = {
            "variables": variables,
            "features": FEATURES_USER,
            "fieldToggles": FIELD_TOGGLES_USER,
        }
        logger.debug("GET %s params=%s", URL_USER_BY_SCREEN_NAME, params)
        resp = await client.get(
            URL_USER_BY_SCREEN_NAME,
            params=params,
            headers=self._headers(),
        )

        if resp.status_code != 200:
            logger.error(
                "UserByScreenName returned HTTP %d\nBody: %s",
                resp.status_code,
                resp.text[:500],
            )
            resp.raise_for_status()

        data = resp.json()

        # Dig through the nested response to find the user's rest_id
        rest_ids = _find_nested(data, "rest_id")
        if not rest_ids:
            logger.error("Full response: %s", json.dumps(data)[:1000])
            raise RuntimeError(f"Could not find user ID for @{self.username}")
        self._user_id = rest_ids[0]
        logger.info("Resolved @%s → user_id=%s", self.username, self._user_id)
        return self._user_id

    async def get_new_tweets(self, since_id: Optional[str] = None, count: int = 20) -> list[Tweet]:
        if not self._ready:
            await self.initialize()

        try:
            async with httpx.AsyncClient(cookies=self._cookies, follow_redirects=True) as client:
                user_id = await self._get_user_id(client)

                variables = json.dumps({
                    "userId": user_id,
                    "count": count,
                    "includePromotedContent": False,
                    "withQuickPromoteEligibilityTweetFields": False,
                    "withVoice": True,
                    "withV2Timeline": True,
                })
                resp = await client.get(
                    URL_USER_TWEETS,
                    params={"variables": variables, "features": FEATURES_TWEETS},
                    headers=self._headers(),
                )

                if resp.status_code != 200:
                    logger.error(
                        "UserTweets returned HTTP %d\nBody: %s",
                        resp.status_code,
                        resp.text[:500],
                    )
                resp.raise_for_status()
                data = resp.json()

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                logger.error(
                    "X cookies expired — re-export from Chrome and save to x_cookies.json"
                )
            else:
                logger.error("X API error %d: %s", e.response.status_code, e)
            return []
        except Exception as e:
            logger.error("Request failed: %s", e)
            return []

        # Parse tweets from the deeply nested GraphQL response
        tweets: list[Tweet] = []
        try:
            tweet_results = _find_nested(data, "tweet_results")
            for tr in tweet_results:
                if not isinstance(tr, dict):
                    continue
                result = tr.get("result", {})
                legacy = result.get("legacy", {})
                if not legacy:
                    continue

                # Skip retweets
                if legacy.get("retweeted_status_id_str"):
                    continue
                if legacy.get("full_text", "").startswith("RT @"):
                    continue

                tweet_id = legacy.get("id_str") or result.get("rest_id", "")
                text = legacy.get("full_text") or legacy.get("text", "")

                if not tweet_id or not text:
                    continue

                # Filter already-seen
                if since_id and tweet_id <= since_id:
                    continue

                tweets.append(Tweet(
                    id=tweet_id,
                    text=text,
                    author=self.username,
                    created_at=legacy.get("created_at", ""),
                    url=f"https://x.com/{self.username}/status/{tweet_id}",
                ))

        except Exception as e:
            logger.error("Failed to parse tweet response: %s", e)
            return []

        tweets.sort(key=lambda t: t.id)
        logger.info("Found %d new tweet(s)", len(tweets))
        return tweets
