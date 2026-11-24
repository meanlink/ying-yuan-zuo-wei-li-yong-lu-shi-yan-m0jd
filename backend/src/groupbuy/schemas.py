from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

OrderNo = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=600)]


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class SubmitGroupEntry(Input):
    """补录一次团购入座。同一订单号重复提交即分次补录。"""

    order_no: OrderNo
    unit_id: int = Field(gt=0)
    seats: int = Field(gt=0, le=500)
    note: Note = ''
