<!-- 🌟 ULTRA PRO MAX LIVE README by ArslanMD Official -->
<p align="center">
  <img src="https://i.ibb.co/Yf5RnVJh/IVAS-ARSLANMD-BANNER.gif" alt="IVAS SMS API LIVE" width="100%"/>
</p>

<h1 align="center">🔥 IVAS SMS PANEL API 🚀 <img src="https://img.shields.io/badge/LIVE-🔥-red?style=for-the-badge" /></h1>

<p align="center">
  <b>Developed & Maintained by</b>  
  <a href="https://github.com/Arslan-MD" target="_blank">
    <img src="https://readme-typing-svg.herokuapp.com?font=Orbitron&size=24&color=FF0000&center=true&vCenter=true&width=500&lines=👑+ArslanMD+Official+🔥;🚀+The+Mind+Behind+IVAS+API+System;🌐+Always+Up+and+Running+⚡" />
  </a>
</p>

---

## ⚡ Features <img src="https://img.shields.io/badge/LIVE-🔥-red?style=for-the-badge" />

- 🚀 **OTP Retrieval** — Fetch OTPs for any date or number range.
- 📊 **SMS Statistics** — Get total, paid, and unpaid counts with revenue.
- 🔒 **Cookie Auth** — Secure sessions with cookie-based authentication.
- 🧠 **Smart Query Engine** — Retrieve OTPs by date, range, and limit.
- 🧩 **Auto Session Handling** — Detects expired cookies.
- 🧰 **Error Debugging** — Detailed logs for every failure.
- 💨 **Response Compression** — Handles gzip and brotli seamlessly.

---

## 🧩 Requirements <img src="https://img.shields.io/badge/LIVE-🔥-red?style=for-the-badge" />

- Valid `cookies.json` with IVAS authentication  
- Python 3.x  
- Dependencies from `requirements.txt`

---

## ⚙️ Installation <img src="https://img.shields.io/badge/LIVE-🔥-red?style=for-the-badge" />

```bash
git clone https://github.com/Arslan-MD/IvaSms-api.git
cd IvaSms-api
pip install -r requirements.txt
python app.py
```

---

🌐 Hosting on Vercel <img src="https://img.shields.io/badge/LIVE-🔥-red?style=for-the-badge" />

1. Fork the repository 🍴


2. Add valid cookies in cookies.json


3. Deploy on Vercel — automatic build 🔄


4. Access your live endpoint instantly 🌍




---

## API Endpoints 📡

- **Welcome Endpoint**:
  - URL: `/`
  - Method: `GET`
  - Response Type: `application/json`
  - Example Response:
    ```json
    {
      "message": "Welcome to the IVAS SMS API",
      "status": "API is alive",
      "endpoints": {
        "/sms": "Get OTP messages for a specific date (format: DD/MM/YYYY) with optional limit. Example: /sms?date=01/05/2025&limit=10"
      }
    }
    ```

- **SMS Retrieval Endpoint**:
  - URL: `/sms`
  - Method: `GET`
  - Query Parameters:
    - `date` (required): Date in `DD/MM/YYYY` format.
    - `to_date` (optional): End date in `DD/MM/YYYY` format.
    - `limit` (optional): Maximum number of OTP messages to return.
  - Response Type: `application/json`
  - Example Response:
    ```json
    {
      "status": "success",
      "from_date": "01/05/2025",
      "to_date": "Not specified",
      "limit": "Not specified",
      "sms_stats": {
        "count_sms": "50",
        "paid_sms": "30",
        "unpaid_sms": "20",
        "revenue": "25.50"
      },
      "otp_messages": [
        {
          "range": "+1",
          "phone_number": "+1234567890",
          "otp_message": "Your OTP is 123456"
        }
      ]
    }
    ```

## Important Notice ⚠️

- **Do not re-upload this code to GitHub**! If you must share or use it elsewhere, please provide proper credit to the original author, [@ArslanMD Official](https://github.com/Arslan-MD). 🚫
- **Forking Recommended**: Instead of re-uploading, fork this repository to contribute or use it for your projects. This helps maintain the integrity of the original work. 🙌



⚠️ Important Notice <img src="https://img.shields.io/badge/LIVE-🔥-red?style=for-the-badge" />

🚫 Do not re-upload this repository to GitHub!
If you want to use it, please fork it and give credit to @ArslanMD Official.

> 🧠 Respect the creator. Support the original work. 💪




---

🤝 Contributing <img src="https://img.shields.io/badge/LIVE-🔥-red?style=for-the-badge" />

Want to improve it? Fork → Modify → Pull Request 🚀
We welcome all contributors 💫


---

📜 License <img src="https://img.shields.io/badge/LIVE-🔥-red?style=for-the-badge" />

This project is open-source.
Check the LICENSE file for terms 📄


---

💬 Official Links <img src="https://img.shields.io/badge/LIVE-🔥-red?style=for-the-badge" />

🔗 WhatsApp Group: Join Now
📢 WhatsApp Channel: Follow Updates
💬 Contact Developer: Message Me


---

<p align="center">
  <img src="https://readme-typing-svg.herokuapp.com?font=Orbitron&size=24&color=00FF00&center=true&vCenter=true&width=600&lines=🔥+IVAS+SMS+API+LIVE+BY+ARSLANMD+OFFICIAL;💥+Always+Active+%26+Powerful+⚡;🚀+Thank+You+for+Visiting+💚" />
</p><p align="center">
  <img src="https://i.ibb.co/z5WkGcz/arslanmd-footer.gif" width="80%"/>
</p>
