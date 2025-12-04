import random
import aiohttp
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Set


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
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    async def _make_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Асинхронный запрос к GraphQL API"""
        if not self.session:
            self.session = aiohttp.ClientSession()
        
        timeout = aiohttp.ClientTimeout(total=10)
        
        async with self.session.post(
            self.url,
            headers=self.headers,
            json=payload,
            timeout=timeout
        ) as response:
            if response.status != 200:
                text = await response.text()
                print(f"GraphQL HTTP {response.status}: {text}")
                # return {}
                return None
            
            return await response.json()
    
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
                print(f"Unexpected totalNeurons value: {total!r}, {e}")
                return 0

        except Exception as e:
            print(f"Error getting neurons for {self.headers.get('X-Auth-Email')}: {e}")
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
            
            print("Асинхронно получаем список моделей...")
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
                print(f"Ошибка при разборе моделей: {e}")
                return 0
            
            if not model_ids:
                print("Не найдено использованных моделей")
                return 0
            
            print(f"Найдены модели: {list(model_ids)}")
            
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
                
                print(f"Асинхронно получаем данные для модели {model_id}...")
                detail_data = await self._make_request(detail_payload)
                
                if detail_data:
                    try:
                        accounts = detail_data["data"]["viewer"]["accounts"]
                        for account in accounts:
                            groups = account.get("aiInferenceAdaptiveGroups", [])
                            for group in groups:
                                neurons = group["sum"]["totalNeurons"]
                                total_neurons += neurons
                                print(f"Получили данные для модели: {model_id}: {neurons:.2f} neurons")
                    except (KeyError, TypeError) as e:
                        print(f"Ошибка при разборе данных для модели {model_id}: {e}")
            
            return int(total_neurons)
            
        except Exception as e:
            print(f"Error in get_today_neurons_by_models: {e}")
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
            print(f"Error in get_today_usage_count: {e}")
            return 0
