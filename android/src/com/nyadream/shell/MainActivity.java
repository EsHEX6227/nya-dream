package com.nyadream.shell;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.text.InputType;
import android.view.Menu;
import android.view.MenuItem;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.Toast;

/**
 * 喵梦 · 安卓外壳
 * 网页在电脑上跑，这个壳只负责：全屏显示它 + 把通知真正弹到通知栏。
 * - 地址可改（默认是打包时那台电脑的局域网地址），记在 SharedPreferences
 * - 电脑端用的是自签证书，所以只对"我们自己配置的那台机器"放行 SSL 错误
 * - 站外链接交给系统浏览器
 */
public class MainActivity extends Activity {
    private WebView web;
    private SharedPreferences sp;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        sp = Prefs.get(this);
        if (Prefs.home(this).isEmpty()) {
            askHome(true);          // 全新安装：先问电脑在哪，别让用户面对白屏
        }

        web = new WebView(this);
        FrameLayout root = new FrameLayout(this);
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        WebSettings st = web.getSettings();
        st.setJavaScriptEnabled(true);
        st.setDomStorageEnabled(true);
        st.setUseWideViewPort(true);
        st.setLoadWithOverviewMode(true);
        st.setSupportMultipleWindows(false);   // target=_blank 也走同一个 WebView，好接管
        st.setMediaPlaybackRequiresUserGesture(false);
        // WebView 的 UA 里带 "; wv"，哔哩哔哩这类站在 WebView 里会白屏；
        // 去掉这个标记，让它按普通 Chrome 对待（顺带把第三方 Cookie 放开，播放器要用）
        String ua = st.getUserAgentString();
        if (ua != null && ua.contains("; wv")) {
            st.setUserAgentString(ua.replace("; wv", ""));
        }
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, String url) {
                String mine = Prefs.origin(MainActivity.this);
                if (url.startsWith(mine)) return false;          // 自己的页面留在壳里
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                } catch (Exception e) {
                    Toast.makeText(MainActivity.this, "打不开这个链接：" + url, Toast.LENGTH_SHORT).show();
                }
                return true;
            }

            @Override
            public void onReceivedSslError(WebView v, SslErrorHandler h, SslError err) {
                String mine = Prefs.origin(MainActivity.this);
                String u = err == null ? "" : err.getUrl();
                if (mine != null && !mine.isEmpty() && u != null && u.startsWith(mine)) {
                    h.proceed();                                  // 自签证书，只给自己那台机器开绿灯
                } else {
                    h.cancel();
                }
            }

            @Override
            public void onPageFinished(WebView v, String url) {
                // 通知服务要拿同一个登录态去连 /events
                try {
                    Prefs.saveCookie(MainActivity.this, CookieManager.getInstance().getCookie(url));
                } catch (Exception ignore) {
                }
            }
        });
        web.setWebChromeClient(new WebChromeClient());

        askNotificationPermission();

        String home = Prefs.home(this);
        if (home.isEmpty()) {
            askHome(true);
        } else {
            gotoHash(home, hashOf(getIntent()));
            NotifyService.start(this);
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        String h = hashOf(intent);
        if (h != null && web != null) gotoHash(Prefs.home(this), h);
    }

    private String hashOf(Intent it) {
        return it == null ? null : it.getStringExtra("hash");
    }

    private void gotoHash(String home, String hash) {
        if (home == null || home.isEmpty()) return;
        String url = home.replaceAll("/#.*$", "");
        url = url.replaceAll("/$", "");
        web.loadUrl(hash == null || hash.isEmpty() ? url : url + "/#" + hash);
    }

    private void askNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33) {
            try {
                requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 1);
            } catch (Exception ignore) {
            }
        }
    }

    /** 第一次进来（或点了"换地址"）问一句电脑端在哪 */
    private void askHome(final boolean first) {
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        input.setHint("http://192.168.1.5:8787/");
        String cur = Prefs.home(this);
        input.setText(cur);

        new AlertDialog.Builder(this)
                .setTitle(first ? "电脑端在哪台机器上？" : "换个地址")
                .setMessage("① 电脑上双击 NyaDream.exe，别关那个黑窗口\n\n② 窗口里找到「局域网」那一行，形如 http://192.168.x.x:8787/\n\n③ 把那一行原样填到下面（手机要和电脑连同一个 WiFi）")
                .setView(input)
                .setCancelable(false)
                .setPositiveButton("就它了", (d, w) -> {
                    String url = Prefs.normalize(input.getText().toString());
                    if (url.isEmpty()) {
                        askHome(first);
                        return;
                    }
                    Prefs.saveHome(MainActivity.this, url);
                    gotoHash(url, null);
                    NotifyService.start(MainActivity.this);
                })
                .show();
    }

    /**
     * 通知服务用的登录：页面里的登录 Cookie 是 HttpOnly，壳不一定读得到，
     * 所以这里让用户用同一个账号再登一次，专门喂给通知服务。
     */
    private void askLogin() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (getResources().getDisplayMetrics().density * 16);
        box.setPadding(pad, pad / 2, pad, 0);
        final EditText user = new EditText(this);
        user.setHint("用户名（和网页里注册的那个一样）");
        user.setText(Prefs.me(this));
        final EditText pw = new EditText(this);
        pw.setHint("密码");
        pw.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        box.addView(user);
        box.addView(pw);
        new AlertDialog.Builder(this)
                .setTitle("通知用的登录")
                .setMessage("只在这台手机上存一份登录态，给通知服务连电脑端用。收不到通知时来这里登一次。")
                .setView(box)
                .setPositiveButton("登录", (d, w) -> {
                    final String u = user.getText().toString().trim();
                    final String p = pw.getText().toString();
                    new Thread(() -> {
                        final String err = NotifyService.shellLogin(MainActivity.this, u, p);
                        runOnUiThread(() -> {
                            Toast.makeText(MainActivity.this, err == null ? "好了，通知服务重新连上" : err,
                                    Toast.LENGTH_LONG).show();
                            if (err == null) NotifyService.start(MainActivity.this);
                        });
                    }).start();
                })
                .setNegativeButton("取消", null)
                .show();
    }

    @Override
    public boolean onCreateOptionsMenu(Menu m) {
        getMenuInflater().inflate(R.menu.main, m);
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        int id = item.getItemId();
        if (id == R.id.act_home) {
            askHome(false);
        } else if (id == R.id.act_login) {
            askLogin();
        } else if (id == R.id.act_test) {
            NotifyService.test(this);
            Toast.makeText(this, "已发一条测试通知", Toast.LENGTH_SHORT).show();
        } else if (id == R.id.act_reconnect) {
            NotifyService.start(this);
            Toast.makeText(this, "正在重连电脑端 …", Toast.LENGTH_SHORT).show();
        } else if (id == R.id.act_notify_setting) {
            try {
                Intent i = new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS);
                i.putExtra(Settings.EXTRA_APP_PACKAGE, getPackageName());
                startActivity(i);
            } catch (Exception e) {
                Toast.makeText(this, "这条系统里没找到通知设置", Toast.LENGTH_SHORT).show();
            }
        } else if (id == R.id.act_reload) {
            web.reload();
        }
        return true;
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
    }
}
