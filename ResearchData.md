# ResearchData.md - TeraBox Manager Project

This document contains all research, discussions, technical findings, decisions, and iterations made during the development of the TeraBox Manager project.

---

## 1. Project Objective

Create a Telegram bot + Web Dashboard that allows users to automatically save TeraBox share links to their account using the `ndus` cookie, with multiple reliable collection methods and fallback support.

---

## 2. Key Technical Challenges Identified

### 2.1 HttpOnly Cookie Limitation
- TeraBox's `ndus` cookie is `HttpOnly`.
- This means `document.cookie` **cannot** read it via JavaScript.
- This invalidated many early ideas involving iframe auto-capture and proxy injection.

### 2.2 Mobile User Experience Problem
- 90%+ of Telegram users are on mobile.
- Installing browser extensions on mobile is difficult or impossible.
- Manual cookie extraction on mobile is a poor user experience.

### 2.3 Reliability vs Automation Trade-off
- Fully automated login methods (QR Code, mobile emulation, password login) are convenient but **fragile**.
- TeraBox frequently changes internal endpoints and increases security measures.
- Manual methods are more reliable but require user effort.

---

## 3. Research Copies Reviewed

### Research Copy 1: Iframe + Proxy Auto-Capture
- Attempted to use Flask reverse proxy + JavaScript injection to auto-read cookies.
- **Verdict**: Flawed. Cannot read `HttpOnly` cookies via JavaScript.
- **Decision**: Rejected.

### Research Copy 2: Manual + Bookmarklet + Userscript
- Focused on practical, reliable methods.
- Introduced Bookmarklet and Userscript with fallback support.
- **Verdict**: Accepted as primary method.

### Research Copy 3: QR Code Login (Device Code Flow)
- Proposed using TeraBox QR Code login to let the server capture the cookie.
- Used `cloudscraper` and polling.
- **Verdict**: Interesting but high risk due to undocumented APIs.
- **Decision**: Keep as experimental/optional feature only.

### Research Copy 4: Air Explorer Style + Domain Pool + TWA
- Advanced proposal with self-healing domain pool and Telegram Web App.
- **Verdict**: Good ideas (domain pool), but overall too complex and risky.

---

## 4. Final Architecture Decisions

### Chosen Approach (Hybrid)
1. **Primary Methods** (Recommended):
   - Bookmarklet
   - Userscript (with Python → PHP fallback)
   - Manual paste

2. **Secondary Methods** (Experimental):
   - QR Code login via Telegram
   - Mobile emulation login (`/login` command)

3. **Fallback**:
   - Manual cookie input in dashboard
   - HAR file upload support (planned)

### Security Decisions
- `ndus` cookie is encrypted when stored in MySQL.
- API Key protection on `/api/save-cookie`.
- Rate limiting on login endpoints.
- Dashboard protected by simple PIN (for testing) or proper auth (production).

### Storage Strategy
- Primary: Environment variable + MySQL (when enabled)
- Fallback: Local JSON file (`terabox_cookies.json`)
- PHP server also maintains its own JSON file as backup.

---

## 5. Technical Findings

### 5.1 TeraBox API Endpoints
- `/share/transfer` — Main transfer endpoint
- `/share/list` — Get share information
- `gettemplatevariable` — Get `bdstoken`
- Passport QR endpoints — Unstable and undocumented

### 5.2 Domain Pool Importance
- TeraBox frequently rotates or blocks domains.
- Having a self-healing domain pool significantly improves reliability.

### 5.3 Userscript Fallback Logic
- The dual-server Userscript (Python first, PHP fallback) provides excellent resilience.

---

## 6. Files Created

- `main.py` — Main application
- `terabox_helper.py` — Transfer logic
- `README.md` — Complete documentation
- `ResearchData.md` — This file
- `.env.example` — Environment variable template

---

## 7. Known Limitations

- Experimental login methods may break without notice.
- QR Code login depends on undocumented TeraBox APIs.
- Full automation is difficult due to TeraBox's security measures.
- Mobile users still need a reasonably easy way to extract cookies (hence bookmarklet + userscript priority).

---

## 8. Future Improvement Ideas

- Add proper user authentication system
- Support multiple users with separate cookies
- Add transfer history database table
- Improve domain health check frequency
- Add notification system when cookie expires

---

*This document should be kept updated as the project evolves.*