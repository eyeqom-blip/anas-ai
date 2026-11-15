import os
import requests

def generate_premarket_brief(headlines: str = ""):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or "YAHAN_" in api_key:
        return "GEMINI_API_KEY .env me missing hai."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    prompt = f"""
    You are ANAS AI's Risk & Market Structure Analyst for Indian Markets (Nifty, Bank Nifty, Sensex).
    Reply in 3 short bullet points in Hinglish:
    1. Overall Bias (BULLISH / BEARISH / CAUTIOUS_SIDEWAYS)
    2. Major Event/News Risk today (RBI, Fed, CPI, Expiry volatility)
    3. Suggested Risk Mode (NORMAL or HALF_QTY)
    Headlines: {headlines or "Focus on pure price action, PDH/PDL and discipline."}
    """
    try:
        r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=15)
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        return f"Gemini Error: {e}"
