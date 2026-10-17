import os
from flask import Flask, request
from .sentiment import score

app = Flask(__name__)

@app.post('/analyze')
def analyze():
    model_name = os.getenv('MODEL_NAME')
    mood_score = score(request.json['content'])
    return {'mood_score': mood_score, 'model': model_name}
