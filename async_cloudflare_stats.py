import asyncio
import random
import time
import aiohttp
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Set

# 300 req / 5 min per user = 1 req/sec; 1.1s gives ~270 req/5min with headroom
_MIN_INTERVAL = 1.1
_MAX_RETRIES = 3
# shared across all instances, keyed by acc_token (per-user rate limit)
_last_request_times: Dict[str, float] = {}


class CloudflareAIStats:
    def __init__(self, api_key: str, email: str, account_id: str):
        self.url = "https://api.cloudflare.com/client/v4/graphql"
        self.headers = {
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Connection": "close",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
            "X-Auth-Key": api_key,
            "X-Auth-Email": email,
        }
        self.account_id = account_id
        self.session: Optional[aiohttp.ClientSession] = None
        self._rate_key = api_key

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def _make_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.session:
            self.session = aiohttp.ClientSession()

        elapsed = time.monotonic() - _last_request_times.get(self._rate_key, 0.0)
        if elapsed < _MIN_INTERVAL:
            await asyncio.sleep(_MIN_INTERVAL - elapsed)

        timeout = aiohttp.ClientTimeout(total=10)

        for attempt in range(_MAX_RETRIES):
            _last_request_times[self._rate_key] = time.monotonic()
            try:
                async with self.session.post(
                    self.url,
                    headers=self.headers,
                    json=payload,
                    timeout=timeout,
                ) as response:
                    if response.status == 429:
                        retry_after = int(response.headers.get("Retry-After", 10))
                        logging.warning(
                            f"Cloudflare 429 for {self.headers.get('X-Auth-Email')}, "
                            f"retry in {retry_after}s (attempt {attempt + 1}/{_MAX_RETRIES})"
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    if response.status != 200:
                        text = await response.text()
                        logging.error(f"GraphQL HTTP {response.status}: {text}")
                        return None
                    return await response.json()
            except asyncio.TimeoutError:
                logging.error(
                    f"Cloudflare timeout for {self.headers.get('X-Auth-Email')} "
                    f"(attempt {attempt + 1}/{_MAX_RETRIES})"
                )
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)

        return None
    
    async def get_today_total_neurons(self) -> int:
        """
        Асинхронно возвращает totalNeurons за сегодня (число).
        Всегда возвращает int (0 если нет данных, а -1 если ошибка).
        """
        try:
            query = """
            query GetAIInferencesTotalNeurons($accountTag: string, $dateStart: string, $dateEnd: string) {
              viewer {
                accounts(filter: {accountTag: $accountTag}) {
                  aiInferenceAdaptiveGroups(filter: {date_geq: $dateStart, date_leq: $dateEnd}, limit: 1) {
                    sum {
                      totalNeurons
                    }
                  }
                }
              }
            }
            """

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            variables = {
                "accountTag": self.account_id,
                "dateStart": today,
                "dateEnd": today
            }

            payload = {
                "operationName": "GetAIInferencesTotalNeurons",
                "variables": variables,
                "query": query
            }

            result = await self._make_request(payload)
            
            if not result:
                return -1

            accounts = result.get('data', {}).get('viewer', {}).get('accounts') or []
            if accounts == []:
                return 0

            ai_groups = accounts[0].get('aiInferenceAdaptiveGroups') or []
            if ai_groups == []:
                return 0

            total = ai_groups[0].get('sum', {}).get('totalNeurons') or 0

            try:
                return int(total)
            except Exception as e:
                logging.error(f"Unexpected totalNeurons value: {total!r}, {e}")
                return 0

        except Exception as e:
            logging.error(f"Error getting neurons for {self.headers.get('X-Auth-Email')}: {e}")
            return 0
    
    async def get_today_neurons_by_models(self) -> int:
        """
        Асинхронно получает neurons по каждой модели
        """
        try:
            # Шаг 1: Получаем список моделей
            models_query = r"""
            query GetModelsUsedOverTime($accountTag: string!, $datetimeStart: Time, $datetimeEnd: Time) {
              viewer {
                accounts(filter: {accountTag: $accountTag}) {
                  aiInferenceAdaptiveGroups(filter: {datetime_geq: $datetimeStart, datetime_leq: $datetimeEnd}, limit: 10000) {
                    dimensions {
                      modelId
                    }
                  }
                }
              }
            }
            """

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            rand_int1 = int(random.uniform(000, 999))
            datetime_start = f'{today}T00:00:00.{rand_int1}Z'
            
            rand_int2 = int(random.uniform(000, 999))
            datetime_end = f'{today}T23:59:59.{rand_int2}Z'

            models_variables = {
                "accountTag": self.account_id,
                "datetimeStart": datetime_start,
                "datetimeEnd": datetime_end
            }
            
            models_payload = {
                "operationName": "GetModelsUsedOverTime",
                "query": models_query,
                "variables": models_variables
            }
            
            logging.debug("Асинхронно получаем список моделей...")
            models_data = await self._make_request(models_payload)
            
            if not models_data:
                return -1

            # Берем модельки
            model_ids: Set[str] = set()
            try:
                accounts = models_data["data"]["viewer"]["accounts"]
                for account in accounts:
                    groups = account.get("aiInferenceAdaptiveGroups", [])
                    if groups == []:
                        return 0
                    for group in groups:
                        model_id = group["dimensions"]["modelId"]
                        model_ids.add(model_id)
            except KeyError as e:
                logging.error(f"Ошибка при разборе моделей: {e}")
                return 0
            
            if not model_ids:
                return 0
            
            logging.debug(f"Найдены модели: {list(model_ids)}")
            
            # Шаг 2: Для каждой модели получаем нейроны
            total_neurons = 0.0
            
            for model_id in model_ids:
                detail_query = r"""
                query GetAIInferencesCostsGroupByModelsOverTime($accountTag: string!, $datetimeStart: Time, $datetimeEnd: Time, $modelIds: [string]) {
                  viewer {
                    accounts(filter: {accountTag: $accountTag}) {
                      aiInferenceAdaptiveGroups(
                        filter: {datetime_geq: $datetimeStart, datetime_leq: $datetimeEnd, modelId_in: $modelIds}, 
                        limit: 10000
                      ) {
                        sum {
                          totalNeurons
                        }
                        dimensions {
                          modelId
                        }
                      }
                    }
                  }
                }
                """
                
                detail_variables = {
                    "accountTag": self.account_id,
                    "datetimeStart": datetime_start,
                    "datetimeEnd": datetime_end,
                    "modelIds": [model_id]
                }
                
                detail_payload = {
                    "operationName": "GetAIInferencesCostsGroupByModelsOverTime",
                    "query": detail_query,
                    "variables": detail_variables
                }
                
                logging.debug(f"Асинхронно получаем данные для модели {model_id}...")
                detail_data = await self._make_request(detail_payload)
                
                if detail_data:
                    try:
                        accounts = detail_data["data"]["viewer"]["accounts"]
                        for account in accounts:
                            groups = account.get("aiInferenceAdaptiveGroups", [])
                            for group in groups:
                                neurons = group["sum"]["totalNeurons"]
                                total_neurons += neurons
                                logging.debug(f"Получили данные для модели: {model_id}: {neurons:.2f} neurons")
                    except (KeyError, TypeError) as e:
                        logging.error(f"Ошибка при разборе данных для модели {model_id}: {e}")
            
            return int(total_neurons)
            
        except Exception as e:
            logging.error(f"Error in get_today_neurons_by_models: {e}")
            return 0
    
    async def get_today_usage_count(self) -> int:
        """
        Асинхронно получает количество использований AI inference
        """
        try:
            query = r"""
            query GetUsageCount($accountTag: string!, $datetimeStart: Time, $datetimeEnd: Time) {
              viewer {
                accounts(filter: {accountTag: $accountTag}) {
                  aiInferenceAdaptiveGroups(
                    limit: 1, 
                    filter: {datetime_geq: $datetimeStart, datetime_leq: $datetimeEnd}, 
                    orderBy: [count_DESC]
                  ) {
                    count
                  }
                }
              }
            }
            """

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            rand_int1 = int(random.uniform(000, 999))
            datetime_start = f'{today}T00:00:00.{rand_int1}Z'
            
            rand_int2 = int(random.uniform(000, 999))
            datetime_end = f'{today}T23:59:59.{rand_int2}Z'
            
            variables = {
                "accountTag": self.account_id,
                "datetimeStart": datetime_start,
                "datetimeEnd": datetime_end
            }
            
            payload = {
                "operationName": "GetUsageCount",
                "query": query,
                "variables": variables
            }
            
            data = await self._make_request(payload)
            
            if data:
                try:
                    count = data["data"]["viewer"]["accounts"][0]["aiInferenceAdaptiveGroups"][0]["count"]
                    return int(count)
                except (KeyError, IndexError):
                    return 0
            return 0
            
        except Exception as e:
            logging.error(f"Error in get_today_usage_count: {e}")
            return 0

    async def get_last_24h_neurons(self) -> float:
        """
        Возвращает суммарное количество нейронов за последние 24 часа (float).
        При ошибке возвращает -1.0.
        """
        try:
            now = datetime.now(timezone.utc)
            start_time = now - timedelta(hours=24)

            # Форматируем как ISO с миллисекундами (как в дашборде)
            datetime_start = start_time.strftime("%Y-%m-%dT%H:%M:%S.") + f"{start_time.microsecond // 1000:03d}Z"
            datetime_end = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

            query = """
            query GetLast24hNeurons($accountTag: string!, $datetimeStart: Time, $datetimeEnd: Time) {
              viewer {
                accounts(filter: {accountTag: $accountTag}) {
                  aiInferenceAdaptiveGroups(
                    filter: {datetime_geq: $datetimeStart, datetime_leq: $datetimeEnd}
                    limit: 10000
                  ) {
                    sum {
                      totalNeurons
                    }
                  }
                }
              }
            }
            """

            variables = {
                "accountTag": self.account_id,
                "datetimeStart": datetime_start,
                "datetimeEnd": datetime_end
            }

            payload = {
                "operationName": "GetLast24hNeurons",
                "query": query,
                "variables": variables
            }

            result = await self._make_request(payload)
            if not result:
                return -1.0

            accounts = result.get('data', {}).get('viewer', {}).get('accounts') or []
            total = 0.0
            for account in accounts:
                groups = account.get('aiInferenceAdaptiveGroups') or []
                for group in groups:
                    neurons = group.get('sum', {}).get('totalNeurons')
                    if neurons is not None:
                        try:
                            total += int(neurons)
                        except (TypeError, ValueError):
                            pass
            return total

        except Exception as e:
            logging.error(f"Error getting last 24h neurons for {self.headers.get('X-Auth-Email')}: {e}")
            return -1.0

    async def get_neurons_by_model_breakdown(self) -> Dict[str, int]:
        """Returns {model_id: neurons} for today's usage."""
        try:
            now = datetime.now(timezone.utc)
            start_time = now - timedelta(hours=24)

            datetime_start = start_time.strftime("%Y-%m-%dT%H:%M:%S.") + f"{start_time.microsecond // 1000:03d}Z"
            datetime_end = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

            models_query = r"""
            query GetModelsUsedOverTime($accountTag: string!, $datetimeStart: Time, $datetimeEnd: Time) {
              viewer {
                accounts(filter: {accountTag: $accountTag}) {
                  aiInferenceAdaptiveGroups(filter: {datetime_geq: $datetimeStart, datetime_leq: $datetimeEnd}, limit: 10000) {
                    dimensions { modelId }
                  }
                }
              }
            }
            """

            models_data = await self._make_request({
                "operationName": "GetModelsUsedOverTime",
                "query": models_query,
                "variables": {
                    "accountTag": self.account_id,
                    "datetimeStart": datetime_start,
                    "datetimeEnd": datetime_end,
                },
            })

            if not models_data:
                return {}

            model_ids: Set[str] = set()
            try:
                for account in models_data["data"]["viewer"]["accounts"]:
                    for group in account.get("aiInferenceAdaptiveGroups", []):
                        model_ids.add(group["dimensions"]["modelId"])
            except (KeyError, TypeError):
                return {}

            if not model_ids:
                return {}

            detail_query = r"""
            query GetAIInferencesCostsGroupByModelsOverTime($accountTag: string!, $datetimeStart: Time, $datetimeEnd: Time, $modelIds: [string]) {
              viewer {
                accounts(filter: {accountTag: $accountTag}) {
                  aiInferenceAdaptiveGroups(
                    filter: {datetime_geq: $datetimeStart, datetime_leq: $datetimeEnd, modelId_in: $modelIds},
                    limit: 10000
                  ) {
                    sum { totalNeurons }
                    dimensions { modelId }
                  }
                }
              }
            }
            """

            result: Dict[str, int] = {}
            for model_id in model_ids:
                data = await self._make_request({
                    "operationName": "GetAIInferencesCostsGroupByModelsOverTime",
                    "query": detail_query,
                    "variables": {
                        "accountTag": self.account_id,
                        "datetimeStart": datetime_start,
                        "datetimeEnd": datetime_end,
                        "modelIds": [model_id],
                    },
                })
                if data:
                    try:
                        for account in data["data"]["viewer"]["accounts"]:
                            for group in account.get("aiInferenceAdaptiveGroups", []):
                                result[model_id] = result.get(model_id, 0) + int(group["sum"]["totalNeurons"])
                    except (KeyError, TypeError, ValueError) as e:
                        logging.error(f"Error parsing model {model_id}: {e}")

            return result

        except Exception as e:
            logging.error(f"Error in get_neurons_by_model_breakdown for {self.headers.get('X-Auth-Email')}: {e}")
            return {}
