"""
Config Router - System configuration management

Uses ConfigService for business logic and CredentialsService for credentials.

Includes:
- Quality thresholds
- Operator preferences
- Credentials management (Brain, LLM API)
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field

from backend.database import get_db
from backend.services.config_service import (
    ConfigService,
    ThresholdsConfig as ThresholdsConfigData,
    DiversityConfig as DiversityConfigData,
)
from backend.services.credentials_service import (
    CredentialsService,
    CredentialKey,
    get_credentials_service,
)

router = APIRouter(
    prefix="/config",
    tags=["config"],
    responses={404: {"description": "Not found"}},
)


# =============================================================================
# DEPENDENCY INJECTION
# =============================================================================

def get_config_service(db: AsyncSession = Depends(get_db)) -> ConfigService:
    """Get ConfigService instance with injected dependencies."""
    return ConfigService(db)


# =============================================================================
# REQUEST/RESPONSE MODELS
# =============================================================================

class ThresholdsConfig(BaseModel):
    sharpe_min: float = 1.5
    turnover_max: float = 0.7
    fitness_min: float = 1.0
    returns_min: float = 0.0
    max_dd_max: float = 0.3


class DiversityConfig(BaseModel):
    max_correlation: float = 0.7


class FullConfig(BaseModel):
    quality_thresholds: Optional[ThresholdsConfig] = None
    diversity_thresholds: Optional[DiversityConfig] = None
    daily_budget: Optional[dict] = None


class BrainCredentialsRequest(BaseModel):
    """Request model for Brain platform credentials."""
    email: str = Field(..., description="Brain platform email")
    password: str = Field(..., description="Brain platform password")


class LLMCredentialsRequest(BaseModel):
    """Request model for LLM API credentials."""
    api_key: str = Field(..., description="API key (e.g., OpenAI, DeepSeek)")
    base_url: str = Field(
        default="https://api.deepseek.com/v1",
        description="API base URL"
    )
    model: str = Field(
        default="deepseek-chat",
        description="Model name"
    )


class CredentialStatusResponse(BaseModel):
    """Response model for credential status."""
    key: str
    masked: str
    is_set: bool
    source: Optional[str] = None
    updated_at: Optional[str] = None


class LLMProviderRequest(BaseModel):
    """Upsert a named LLM provider profile (endpoint + key). The secret api_key
    is stored encrypted in CredentialsService (key llm_provider_<name>); the
    endpoint metadata goes into the LLM_PROVIDERS json feature-flag."""
    name: str = Field(..., description="厂商唯一标识 (slug, 如 moonshot / aliyun_maas)")
    label: str = Field(default="", description="展示名 (如 Moonshot官方)")
    sdk: str = Field(default="openai", description="SDK 类型: openai | anthropic")
    base_url: str = Field(default="", description="API base URL (anthropic 留空=官方默认)")
    api_key: Optional[str] = Field(
        default=None,
        description="API 密钥;留空=保留已存密钥不变 (编辑时)",
    )


class LLMProviderStatus(BaseModel):
    name: str
    label: str
    sdk: str
    base_url: str
    has_key: bool


class OperatorPrefResponse(BaseModel):
    operator_name: str
    status: str
    usage_count: int
    success_count: int
    failure_rate: float


# =============================================================================
# CREDENTIALS MANAGEMENT (Must be before /{key} route)
# =============================================================================

@router.get("/credentials")
async def get_credentials_status(db: AsyncSession = Depends(get_db)):
    """Get status of all configured credentials (masked values)."""
    service = get_credentials_service(db)
    credentials = await service.get_all_credentials_masked()
    
    return {
        "credentials": credentials,
        "message": "Use POST endpoints to update credentials"
    }


@router.post("/credentials/brain")
async def set_brain_credentials(
    credentials: BrainCredentialsRequest,
    db: AsyncSession = Depends(get_db)
):
    """Set WorldQuant Brain platform credentials."""
    from backend.adapters.brain_adapter import BrainAdapter
    
    service = get_credentials_service(db)
    
    try:
        await service.set_credential(
            CredentialKey.BRAIN_EMAIL,
            credentials.email,
            description="WorldQuant Brain platform email"
        )
        await service.set_credential(
            CredentialKey.BRAIN_PASSWORD,
            credentials.password,
            description="WorldQuant Brain platform password"
        )
        
        # Invalidate cached credentials
        BrainAdapter.invalidate_credentials_cache()
        CredentialsService.invalidate_cache()
        
        return {
            "success": True,
            "message": "Brain credentials saved successfully"
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save credentials: {str(e)}"
        )


@router.post("/credentials/llm")
async def set_llm_credentials(
    credentials: LLMCredentialsRequest,
    db: AsyncSession = Depends(get_db)
):
    """Set LLM API credentials (OpenAI, DeepSeek, etc.)."""
    service = get_credentials_service(db)
    
    try:
        await service.set_credential(
            CredentialKey.OPENAI_API_KEY,
            credentials.api_key,
            description="LLM API key"
        )
        await service.set_credential(
            CredentialKey.OPENAI_BASE_URL,
            credentials.base_url,
            description="LLM API base URL"
        )
        await service.set_credential(
            CredentialKey.OPENAI_MODEL,
            credentials.model,
            description="LLM model name"
        )
        
        # Invalidate credential caches
        CredentialsService.invalidate_cache()
        try:
            from backend.agents.services.llm_service import get_llm_service
            get_llm_service().invalidate_credentials_cache()
        except Exception:
            pass
        
        return {
            "success": True,
            "message": "LLM credentials saved successfully"
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save credentials: {str(e)}"
        )


@router.post("/credentials/brain/test")
async def test_brain_credentials(db: AsyncSession = Depends(get_db)):
    """Test Brain platform credentials by attempting authentication."""
    service = get_credentials_service(db)
    result = await service.test_brain_credentials()
    
    if not result["success"]:
        raise HTTPException(
            status_code=400,
            detail=result.get("error", "Authentication failed")
        )
    
    return result


@router.delete("/credentials/{key}")
async def delete_credential(
    key: str,
    db: AsyncSession = Depends(get_db)
):
    """Delete a specific credential."""
    valid_keys = [
        CredentialKey.BRAIN_EMAIL,
        CredentialKey.BRAIN_PASSWORD,
        CredentialKey.OPENAI_API_KEY,
        CredentialKey.OPENAI_BASE_URL,
        CredentialKey.OPENAI_MODEL,
    ]
    
    if key not in valid_keys:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid credential key. Valid keys: {valid_keys}"
        )
    
    service = get_credentials_service(db)
    deleted = await service.delete_credential(key)
    
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Credential '{key}' not found"
        )
    
    return {"success": True, "message": f"Credential '{key}' deleted"}


# =============================================================================
# LLM PROVIDER REGISTRY (named endpoint+key profiles for per-function routing)
# =============================================================================
# A provider profile = pre-configured {label, sdk, base_url} stored in the
# LLM_PROVIDERS json feature-flag; the secret api_key lives encrypted in
# CredentialsService under credential key ``llm_provider_<name>`` and is resolved
# at call time via the routing entry's derived api_key_ref. The ops LLM-Routing
# console references a provider by name (provider_ref) instead of typing the
# endpoint/key inline. See backend/agents/services/llm_service.py:_expand_provider_ref.

_PROVIDER_FLAG = "LLM_PROVIDERS"


def _read_provider_registry() -> Dict[str, Dict[str, Any]]:
    """Effective LLM_PROVIDERS map: runtime override wins, else startup default."""
    from backend.config import _flag_override_cache, _LLM_PROVIDERS_CACHE
    override = _flag_override_cache.get(_PROVIDER_FLAG)
    reg = override if override is not None else _LLM_PROVIDERS_CACHE
    return dict(reg) if isinstance(reg, dict) else {}


def _provider_cred_key(name: str) -> str:
    return f"llm_provider_{name}"


@router.get("/llm-providers", response_model=List[LLMProviderStatus])
async def list_llm_providers(db: AsyncSession = Depends(get_db)):
    """List configured LLM providers with key-set status (keys never returned)."""
    service = get_credentials_service(db)
    registry = _read_provider_registry()
    out: List[LLMProviderStatus] = []
    for name, prof in registry.items():
        if not isinstance(prof, dict):
            continue
        key_val = await service.get_credential(_provider_cred_key(name))
        out.append(LLMProviderStatus(
            name=name,
            label=str(prof.get("label") or name),
            sdk=str(prof.get("sdk") or prof.get("provider") or "openai"),
            base_url=str(prof.get("base_url") or ""),
            has_key=bool(key_val),
        ))
    out.sort(key=lambda p: p.name)
    return out


@router.post("/llm-providers")
async def upsert_llm_provider(
    payload: LLMProviderRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create or update a named LLM provider. Writes the endpoint metadata to the
    LLM_PROVIDERS feature-flag and (if api_key supplied) the encrypted secret."""
    from backend.services.feature_flag_service import FeatureFlagService

    name = (payload.name or "").strip()
    if not name or not all(c.isalnum() or c in ("_", "-") for c in name):
        raise HTTPException(
            status_code=400,
            detail="name 必须非空且仅含字母/数字/下划线/连字符",
        )
    if payload.sdk not in ("openai", "anthropic"):
        raise HTTPException(status_code=400, detail="sdk 必须是 openai 或 anthropic")

    cred_service = get_credentials_service(db)

    # Secret: store only when provided. On edit with blank api_key, keep existing.
    if payload.api_key:
        await cred_service.set_credential(
            _provider_cred_key(name),
            payload.api_key,
            description=f"LLM provider API key ({name})",
        )

    # Merge the profile into the registry and persist the flag.
    registry = _read_provider_registry()
    registry[name] = {
        "label": (payload.label or name).strip(),
        "sdk": payload.sdk,
        "base_url": (payload.base_url or "").strip(),
    }
    flag_service = FeatureFlagService(db)
    await flag_service.set(_PROVIDER_FLAG, registry, actor="config_center")

    # Invalidate caches so this process re-resolves immediately; worker processes
    # pick up the flag via their 60s refresher and the secret via CredentialsService.
    CredentialsService.invalidate_cache()
    try:
        from backend.agents.services.llm_service import get_llm_service
        get_llm_service().invalidate_credentials_cache()
    except Exception:
        pass

    has_key = bool(await cred_service.get_credential(_provider_cred_key(name)))
    return {
        "success": True,
        "name": name,
        "has_key": has_key,
        "message": f"厂商「{name}」已保存。worker 进程 ≤60s 内同步路由配置。",
    }


@router.delete("/llm-providers/{name}")
async def delete_llm_provider(
    name: str,
    db: AsyncSession = Depends(get_db),
):
    """Remove a provider profile + its stored secret."""
    from backend.services.feature_flag_service import FeatureFlagService

    registry = _read_provider_registry()
    if name not in registry:
        raise HTTPException(status_code=404, detail=f"厂商「{name}」不存在")

    del registry[name]
    flag_service = FeatureFlagService(db)
    await flag_service.set(_PROVIDER_FLAG, registry, actor="config_center")

    cred_service = get_credentials_service(db)
    await cred_service.delete_credential(_provider_cred_key(name))
    CredentialsService.invalidate_cache()

    return {"success": True, "message": f"厂商「{name}」已删除"}


# =============================================================================
# THRESHOLDS & DIVERSITY
# =============================================================================

@router.get("/thresholds")
async def get_thresholds(
    service: ConfigService = Depends(get_config_service),
):
    """Get quality thresholds configuration."""
    config = await service.get_thresholds()
    return {
        "sharpe_min": config.sharpe_min,
        "turnover_max": config.turnover_max,
        "fitness_min": config.fitness_min,
        "returns_min": config.returns_min,
        "max_dd_max": config.max_dd_max,
    }


@router.put("/thresholds")
async def update_thresholds(
    thresholds: ThresholdsConfig,
    service: ConfigService = Depends(get_config_service),
):
    """Update quality thresholds."""
    config = ThresholdsConfigData(
        sharpe_min=thresholds.sharpe_min,
        turnover_max=thresholds.turnover_max,
        fitness_min=thresholds.fitness_min,
        returns_min=thresholds.returns_min,
        max_dd_max=thresholds.max_dd_max,
    )
    
    updated = await service.update_thresholds(config)
    
    return {
        "message": "Thresholds updated",
        "thresholds": {
            "sharpe_min": updated.sharpe_min,
            "turnover_max": updated.turnover_max,
            "fitness_min": updated.fitness_min,
            "returns_min": updated.returns_min,
            "max_dd_max": updated.max_dd_max,
        }
    }


@router.put("/diversity")
async def update_diversity(
    diversity: DiversityConfig,
    service: ConfigService = Depends(get_config_service),
):
    """Update diversity thresholds."""
    config = DiversityConfigData(
        max_correlation=diversity.max_correlation,
    )
    
    updated = await service.update_diversity_config(config)
    
    return {
        "message": "Diversity config updated",
        "diversity": {
            "max_correlation": updated.max_correlation,
        }
    }


# =============================================================================
# OPERATORS
# =============================================================================

@router.get("/operators", response_model=List[OperatorPrefResponse])
async def get_operator_prefs(
    service: ConfigService = Depends(get_config_service),
):
    """Get all operator preferences."""
    prefs = await service.get_operator_preferences()
    
    return [
        OperatorPrefResponse(
            operator_name=p.operator_name,
            status=p.status,
            usage_count=p.usage_count,
            success_count=p.success_count,
            failure_rate=p.failure_rate,
        )
        for p in prefs
    ]


@router.put("/operators/{operator_name}")
async def update_operator_pref(
    operator_name: str,
    status: str,
    service: ConfigService = Depends(get_config_service),
):
    """Update operator status (ACTIVE, BANNED, DEPRECATED)."""
    try:
        await service.update_operator_status(operator_name, status)
        return {"message": f"Operator {operator_name} set to {status}"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# =============================================================================
# GENERAL CONFIG (Must be last due to {key} path param)
# =============================================================================

@router.get("")
async def get_all_config(
    service: ConfigService = Depends(get_config_service),
):
    """Get all system configuration (excluding credentials)."""
    return await service.get_all_config()
