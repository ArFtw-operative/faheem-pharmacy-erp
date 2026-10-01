"""Configured unit hierarchies; conversion uses product stock units, not size text."""
from decimal import Decimal, InvalidOperation
from sqlalchemy import select
from app.models import ItemUom


def definitions(db,item):
    return {row.unit:(row.parent_unit,row.factor) for row in db.scalars(select(ItemUom).where(ItemUom.item_id==item.id))}


def factor(item,unit,definitions=None):
    unit=str(unit or 'BASE').strip().upper();graph=definitions or {}
    defaults={'BASE':1,item.base_unit:1,'PACK':item.units_per_pack or 1,item.pack_unit:item.units_per_pack or 1}
    def resolve(name,path):
        if name in path: raise ValueError('Unit hierarchy contains a cycle')
        if name in graph:
            parent,multiplier=graph[name]
            if multiplier<1: raise ValueError('Unit conversion must be positive')
            return multiplier*resolve(parent,path|{name})
        if name not in defaults: raise ValueError('Unit is not configured for this product: '+name)
        return defaults[name]
    return resolve(unit,set())


def to_base(db,item,quantity,unit):
    try: qty=Decimal(str(quantity))
    except (InvalidOperation,ValueError): raise ValueError('Quantity must be a valid number')
    multiplier=factor(item,unit,definitions(db,item));base=qty*multiplier
    if not base.is_finite() or base<=0 or base!=base.to_integral_value(): raise ValueError('Quantity must resolve to whole stock units')
    return int(base),multiplier


def validate(item,graph):
    for unit in graph: factor(item,unit,graph)
    if factor(item,item.base_unit,graph)!=1 or factor(item,item.pack_unit,graph)!=(item.units_per_pack or 1):
        raise ValueError('Hierarchy must agree with the product base unit and purchase pack configuration')
