import io

p = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\lejian.py"
s = io.open(p, encoding="utf-8").read()
orig = s

s = s.replace(
    "def _request(method, path, params=None, body=None, form=False, auth=True,\n"
    "             token=None, school_id=None):",
    "def _request(method, path, params=None, body=None, form=False, auth=True,\n"
    "             token=None, school_id=None, timeout=15):")

s = s.replace("urllib.request.urlopen(req, timeout=15, context=SSL_CTX)",
              "urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)")

s = s.replace(
    '    def post(self, path, body):\n'
    '        self.ensure_token()\n'
    '        return _request("POST", path, body=body,\n'
    '                        token=self.s["accessToken"], school_id=self.s["schoolId"])\n'
    '\n'
    '    def get(self, path, params=None):\n'
    '        self.ensure_token()\n'
    '        return _request("GET", path, params=params,\n'
    '                        token=self.s["accessToken"], school_id=self.s["schoolId"])\n'
    '\n'
    '    def call(self, method, path, body=None):\n'
    '        code, text = (self.post(path, body) if method == "POST"\n'
    '                      else self.get(path, body))',
    '    def post(self, path, body, timeout=15):\n'
    '        self.ensure_token()\n'
    '        return _request("POST", path, body=body, timeout=timeout,\n'
    '                        token=self.s["accessToken"], school_id=self.s["schoolId"])\n'
    '\n'
    '    def get(self, path, params=None, timeout=15):\n'
    '        self.ensure_token()\n'
    '        return _request("GET", path, params=params, timeout=timeout,\n'
    '                        token=self.s["accessToken"], school_id=self.s["schoolId"])\n'
    '\n'
    '    def call(self, method, path, body=None, timeout=15):\n'
    '        code, text = (self.post(path, body, timeout) if method == "POST"\n'
    '                      else self.get(path, body, timeout))')

s = s.replace(
    '    def activities(self, page_size=100):\n'
    '        """拉全部报名中的活动（分页）"""\n'
    '        out, page = [], 1\n'
    '        while page <= 10:\n'
    '            code, obj = self.call("POST", "education/app/activity/getActivityList",\n'
    '                                  {"state": 1, "page": page, "size": page_size,\n'
    '                                   "semesterId": self.s["semesterId"]})',
    '    def activities(self, page_size=200, req_timeout=6):\n'
    '        """一次拉 200 条，避免分页多次往返"""\n'
    '        out, page = [], 1\n'
    '        while page <= 10:\n'
    '            code, obj = self.call("POST", "education/app/activity/getActivityList",\n'
    '                                  {"state": 1, "page": page, "size": page_size,\n'
    '                                   "semesterId": self.s["semesterId"]},\n'
    '                                  timeout=req_timeout)')

if s == orig:
    print("!! 没有任何替换生效")
else:
    io.open(p, "w", encoding="utf-8").write(s)
    print("patched, changes:", sum(1 for a, b in zip(orig.splitlines(), s.splitlines()) if a != b), "lines")
