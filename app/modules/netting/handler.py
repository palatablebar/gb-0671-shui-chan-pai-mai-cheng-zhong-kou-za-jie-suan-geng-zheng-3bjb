from fastapi import APIRouter, Request, Body
from app.engine import evaluate
from app.web import render
router=APIRouter()
@router.get('/netting')
def page(request:Request):
    return render(request,'netting')
@router.post('/api/netting')
def calculate(payload:dict=Body(default={})):
    return evaluate(payload,until=7)
