from pydantic import BaseModel, field_validator
from datetime import datetime
from typing import Optional

VALID_DOMAINS = ["供应链","采购","财务","医疗","金融","法律","教育","科技","制造","能源","其他"]

class OntologyCreate(BaseModel):
    name: str
    domain: str
    description: Optional[str] = None
    build_mode: Optional[str] = "simple_llm"

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, v):
        if v not in VALID_DOMAINS:
            raise ValueError(f"Domain must be one of: {VALID_DOMAINS}")
        return v

class OntologyUpdate(BaseModel):
    name: Optional[str] = None
    domain: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    version: Optional[str] = None
    build_mode: Optional[str] = None

class OntologyOut(BaseModel):
    id: str
    name: str
    domain: str
    description: Optional[str]
    version: str
    status: str
    build_mode: Optional[str] = "simple_llm"
    is_dirty: bool = False
    created_by: str
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}

class OntologyListItem(BaseModel):
    id: str
    name: str
    domain: str
    version: str
    status: str
    build_mode: Optional[str] = "simple_llm"
    entity_count: int = 0
    relation_count: int = 0
    created_by: str
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}
