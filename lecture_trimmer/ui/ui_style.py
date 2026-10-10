"""The sign-in page shown before the studio opens."""
from lecture_trimmer.ui.branding import logo

LOGIN_STYLE = """<style>*{box-sizing:border-box}body{margin:0;background:#f4f6fb;color:#172033;font:14px 'Segoe UI',Arial,sans-serif;display:grid;place-items:center;min-height:100vh;padding:24px}.card{width:100%;max-width:460px;background:#fff;border:1px solid #e2e8f0;border-radius:20px;padding:36px;box-shadow:0 14px 50px #3341550a}.mark{color:#4f46e5;font-size:32px}h1{font-size:27px;letter-spacing:-1px}p{line-height:1.7;color:#64748b}button,a.button{border:0;border-radius:10px;background:#4f46e5;color:white;padding:13px 18px;width:100%;font:600 14px 'Segoe UI';cursor:pointer;text-decoration:none;display:inline-block;text-align:center}.caption{font-size:12px;margin-top:24px}</style>"""

def login_page(control):
    return ('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Lecture Trimmer</title><link rel="icon" href="/favicon.ico">'+LOGIN_STYLE+'</head><body><main class="card">'
            '<div class="mark">'+logo()+'</div><h1>Make time for learning.</h1>'
            '<p>Turn a lecture into focused question-solving, theory, and combined study videos.</p>'
            + control + '<p class="caption">Lecture Trimmer · Access is managed by your organisation.</p></main></body></html>')
