from pydantic import BaseModel, Field


class AccountSuccessResponse(BaseModel):
    """Модель для успешного ответа с данными аккаунта"""
    status: str = Field(default="success", description="Статус операции")
    account_id: str = Field(..., description="ID аккаунта Cloudflare")
    ai_token: str = Field(..., description="Токен для AI операций на Cloudflare")
    neurons_count: int = Field(..., ge=0, description="Количество использованных нейронов")
    email: str = Field(..., description="Email пользователя")

    class Config:
        json_schema_extra = {
            "example": {
                "status": "success",
                "account_id": "f99c208a0d75deac711d68ccfe7c02b9",
                "ai_token": "BTTxFsILyDglZI59M79ISYeaSDfmi47M5ePturAA",
                "neurons_count": 5420,
                "email": "lkznilsp@pineapple-berry.pro"
            }
        }


class AccountNoAccountsResponse(BaseModel):
    """Модель для случая, когда нет доступных аккаунтов"""
    status: str = Field(default="no_accounts", description="Статус операции")
    message: str = Field(..., description="Сообщение об ошибке")

    class Config:
        json_schema_extra = {
            "example": {
                "status":"no_accounts",
                "message":"No accounts with neurons < 10000 found"
            }
        }
