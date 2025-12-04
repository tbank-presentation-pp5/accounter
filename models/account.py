from pydantic import BaseModel, Field
from typing import Optional


class AccountDataInput(BaseModel):
    email: str = Field(..., description="Email пользователя")
    password: Optional[str] = Field(
        None,
        description="Пароль"
    )
    acc_token: str = Field(
        ...,
        min_length=37,
        description="API токен Cloudflare (Global API Token, X-Auth-Key)"
    )
    account_id: str = Field(
        ...,
        min_length=32,
        description="ID аккаунта Cloudflare"
    )
    ai_token: str = Field(
        ...,
        min_length=40,
        description="Токен для AI операций (AI Token)"
    )
    
    class Config:
        json_schema_extra = {
            "example": {
                "email": "ignpvyft@privacy-mail.info",
                "password": "E!aQuRv-~9SEp229",
                "acc_token": "6e9ae3d00717099a32844698e3deef266743a",
                "account_id": "e03ce422932ac71914227a7a2760d436",
                "ai_token": "6qpvsEjuAqi7i5-fDDa3Cz8jr6s9cPnrUTW2mYkI"
            }
        }


class AccountAddResponse(BaseModel):
    """Модель для ответа /add_account"""
    status: str
    message: str

    class Config:
        json_schema_extra = {
            "example": {
              "status": "success",
              "message": "Account added/updated"
            }
        }
