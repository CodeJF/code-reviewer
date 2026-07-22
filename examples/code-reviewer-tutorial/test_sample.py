import json
import os

def read_config(path):
    f = open(path)
    data = json.load(f)
    return data

def process_users(users):
    result = []
    for i in range(len(users)):
        name = users[i]["name"]
        age = users[i]["age"]
        if age > 0:
            result.append(name)
    return result

def delete_file(path):
    os.remove(path)

password = "admin123"

API_KEY = "sk-1234567890abcdef"
