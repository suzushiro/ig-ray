/**
 * insta-ray / dev/lightbox_check.js
 *
 * ライトボックスのキーボード操作を検証する。
 *   左右 … 同じ投稿内の画像切り替え（ループする）
 *   上下 … 前後の投稿へ移動（端では止まる）
 *
 *   npm i -D jsdom && node dev/lightbox_check.js
 * jsdom が無い環境では自前の最小DOMで代替する。
 */

const fs = require('fs');
const path = require('path');

const PASS = [], FAIL = [];
function check(name, cond, detail) {
    (cond ? PASS : FAIL).push(name);
    console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${name}${!cond && detail ? '  -- ' + detail : ''}`);
}

// _scripts.html から最初の <script> ブロック（ライトボックス本体）を取り出す
const html = fs.readFileSync(
    path.join(__dirname, '..', 'app', 'templates', '_scripts.html'), 'utf8');
const blocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const code = blocks.join('\n');

let JSDOM;
try {
    ({ JSDOM } = require('jsdom'));
} catch (e) {
    console.error('jsdom が必要です: npm i -D jsdom');
    process.exit(2);
}

// b64: 日本語ハンドルも通るように encodeURIComponent 経由
const b64 = o => Buffer.from(JSON.stringify(o)).toString('base64');

// テンプレートには Jinja のタグが混ざりうる。eval する前に落とす。
// （{% if %} が <script> に入ると Unexpected token '%' で落ちる）
function stripJinja(src) {
    return src.replace(/\{%[\s\S]*?%\}/g, '')
              .replace(/\{\{[\s\S]*?\}\}/g, '');
}

function buildDom(kind) {
    let body;
    if (kind === 'feed') {
        // 3投稿: 1枚 / 3枚 / 1枚
        body = `
        <div class="tweet-media" data-imgs="${b64(['a0.jpg'])}" data-handle="userA" data-dt="202607010000">
            <div class="media-cell"><img src="a0.jpg" data-idx="0"></div>
        </div>
        <div class="tweet-media" data-imgs="${b64(['b0.jpg','b1.jpg','b2.jpg'])}" data-handle="userB" data-dt="202607020000">
            <div class="media-cell"><img src="b0.jpg" data-idx="0"></div>
            <div class="media-cell"><img src="b1.jpg" data-idx="1"></div>
            <div class="media-cell"><img src="b2.jpg" data-idx="2"></div>
        </div>
        <div class="tweet-media" data-imgs="${b64(['c0.jpg'])}" data-handle="userC" data-dt="202607030000">
            <div class="media-cell"><img src="c0.jpg" data-idx="0"></div>
        </div>
        <div class="tweet-media" data-imgs="${b64(['v0.jpg','v1.jpg'])}"
             data-vids="${b64([null, 'https://www.instagram.com/p/VID1/'])}"
             data-handle="userV" data-dt="202607040000">
            <div class="media-cell"><img src="v0.jpg" data-idx="0"></div>
            <div class="media-cell"><img src="v1.jpg" data-idx="1"></div>
        </div>`;
    } else {
        // ギャラリー: 1アイテム=1画像。3列×2行を想定した6枚
        body = [...Array(6)].map((_, i) => `
        <div class="gallery-item" data-imgs="${b64([`g${i}.jpg`])}" data-idx="0"
             data-handle="user${i}" data-tid="SC00${i}" data-fidx="${i + 1}"
             data-dt="2026-07-0${i + 1}">
            <img src="g${i}.jpg" data-idx="0"></div>`).join('');
    }

    const dom = new JSDOM(`<!DOCTYPE html><html><body>
        <div class="dlg-wrap" id="mute-dlg">
            <span id="mute-dlg-user"></span>
            <input type="text" id="mute-dlg-reason">
            <div id="mute-dlg-msg"></div>
            <button id="mute-dlg-ok"></button>
        </div>
        <div class="tweet" data-owner="userA"><span>postA1</span></div>
        <div class="tweet" data-owner="userA"><span>postA2</span></div>
        <div class="tweet" data-owner="userB"><span>postB1</span></div>
        <div id="lb">
            <a id="lb-play" href="#" style="display:none"></a>
            <div id="lb-body">
            <img id="lb-img">
            <button class="lb-prev"></button><button class="lb-next"></button>
            <span id="lb-counter"></span>
            <button id="lb-dl"></button>
        </div></div>
        ${body}
        </body></html>`, {
            runScripts: 'outside-only',
            pretendToBeVisual: true,
            // about:blank だと localStorage が SecurityError を投げるのでURLを与える
            url: 'http://localhost/',
        });

    // jsdom に無いAPIを埋める（本体コードは無限スクロールで使う）
    dom.window.Element.prototype.scrollIntoView = function () { this._scrolled = true; };
    dom.window.IntersectionObserver = class {
        constructor(cb) { this.cb = cb; }
        observe() {} unobserve() {} disconnect() {}
    };
    dom.window.localStorage.setItem('instaray-theme', 'light');
    dom.window.eval(stripJinja(code));
    return dom;
}

function press(dom, key) {
    const ev = new dom.window.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true });
    dom.window.document.dispatchEvent(ev);
    return ev;
}
const shown = dom => dom.window.document.getElementById('lb-img').getAttribute('src');
const isOpen = dom => dom.window.document.getElementById('lb').classList.contains('open');

// ---------------------------------------------------------------- [1]
console.log('\n[1] 左右キー: 投稿内の画像切り替え');
{
    const dom = buildDom('feed');
    const d = dom.window.document;
    const posts = d.querySelectorAll('.tweet-media');
    // 2番目の投稿（3枚）の1枚目を開く
    dom.window.imgClick(posts[1].querySelectorAll('img')[0]);

    check('開くとライトボックスが表示される', isOpen(dom));
    check('クリックした画像が出る', shown(dom) === 'b0.jpg', shown(dom));

    press(dom, 'ArrowRight');
    check('→ で次の画像', shown(dom) === 'b1.jpg', shown(dom));
    press(dom, 'ArrowRight');
    check('もう一度 → で3枚目', shown(dom) === 'b2.jpg', shown(dom));
    press(dom, 'ArrowRight');
    check('末尾で → は先頭へループ', shown(dom) === 'b0.jpg', shown(dom));
    press(dom, 'ArrowLeft');
    check('先頭で ← は末尾へループ', shown(dom) === 'b2.jpg', shown(dom));

    check('カウンタが出る',
          d.getElementById('lb-counter').textContent === '3 / 3',
          d.getElementById('lb-counter').textContent);
}

// ---------------------------------------------------------------- [2]
console.log('\n[2] 上下キー: 投稿の切り替え');
{
    const dom = buildDom('feed');
    const d = dom.window.document;
    const posts = d.querySelectorAll('.tweet-media');
    dom.window.imgClick(posts[0].querySelector('img'));
    check('1投稿目を開く', shown(dom) === 'a0.jpg', shown(dom));

    press(dom, 'ArrowDown');
    check('↓ で次の投稿の先頭画像', shown(dom) === 'b0.jpg', shown(dom));

    press(dom, 'ArrowDown');
    check('もう一度 ↓ で3投稿目', shown(dom) === 'c0.jpg', shown(dom));

    press(dom, 'ArrowDown');
    check('↓ で4投稿目（動画付き）', shown(dom) === 'v0.jpg', shown(dom));

    press(dom, 'ArrowDown');
    check('末尾で ↓ は動かない（ループしない）', shown(dom) === 'v0.jpg', shown(dom));
    check('末尾で ↓ してもライトボックスは開いたまま', isOpen(dom));

    press(dom, 'ArrowUp');
    check('↑ で前の投稿へ戻る', shown(dom) === 'c0.jpg', shown(dom),
          '上へ戻るときは末尾の画像から');

    press(dom, 'ArrowUp');
    check('さらに ↑ で2投稿目の末尾', shown(dom) === 'b2.jpg', shown(dom));
    press(dom, 'ArrowUp');
    check('さらに ↑ で1投稿目', shown(dom) === 'a0.jpg', shown(dom));
    press(dom, 'ArrowUp');
    check('先頭で ↑ は動かない', shown(dom) === 'a0.jpg', shown(dom));
}

// ---------------------------------------------------------------- [3]
console.log('\n[3] 上下と左右の組み合わせ');
{
    const dom = buildDom('feed');
    const d = dom.window.document;
    const posts = d.querySelectorAll('.tweet-media');
    dom.window.imgClick(posts[0].querySelector('img'));

    press(dom, 'ArrowDown');           // 2投稿目 b0
    press(dom, 'ArrowRight');          // b1
    check('投稿移動後も左右が効く', shown(dom) === 'b1.jpg', shown(dom));

    press(dom, 'ArrowDown');           // 3投稿目 c0
    check('途中の画像からでも ↓ で次の投稿', shown(dom) === 'c0.jpg', shown(dom));

    press(dom, 'ArrowUp');             // 2投稿目の末尾 b2
    check('↑ で戻ると末尾画像', shown(dom) === 'b2.jpg', shown(dom));
    check('カウンタも追従する',
          d.getElementById('lb-counter').textContent === '3 / 3',
          d.getElementById('lb-counter').textContent);
}

// ---------------------------------------------------------------- [4]
console.log('\n[4] ギャラリー（1アイテム=1画像）');
{
    const dom = buildDom('gallery');
    const d = dom.window.document;
    const items = d.querySelectorAll('.gallery-item');
    dom.window.imgClick(items[0].querySelector('img'));
    check('1枚目を開く', shown(dom) === 'g0.jpg', shown(dom));

    press(dom, 'ArrowRight');
    check('→ で次のアイテムへ進む', shown(dom) === 'g1.jpg', shown(dom));
    press(dom, 'ArrowLeft');
    check('← で前のアイテムへ戻る', shown(dom) === 'g0.jpg', shown(dom));
    check('単一画像ではカウンタを出さない',
          d.getElementById('lb-counter').textContent === '',
          d.getElementById('lb-counter').textContent);
}

// ---------------------------------------------------------------- [5]
console.log('\n[5] その他のキー / 後片付け');
{
    const dom = buildDom('feed');
    const d = dom.window.document;
    dom.window.imgClick(d.querySelectorAll('.tweet-media')[1].querySelectorAll('img')[0]);

    const ev = press(dom, 'ArrowDown');
    check('矢印キーは既定動作を止める（背面スクロール防止）', ev.defaultPrevented);

    const ev2 = press(dom, 'a');
    check('無関係なキーは既定動作を残す', !ev2.defaultPrevented);

    press(dom, 'Escape');
    check('Escape で閉じる', !isOpen(dom));

    // 閉じた後にキーを押しても何も起きない（リスナーが外れている）
    const before = shown(dom);
    press(dom, 'ArrowDown');
    check('閉じた後はキーが効かない', shown(dom) === before, shown(dom));
}

// ---------------------------------------------------------------- [6]
console.log('\n[6] 壊れたデータでも落ちない');
{
    const dom = buildDom('feed');
    const d = dom.window.document;
    const posts = d.querySelectorAll('.tweet-media');
    // 2投稿目の data-imgs を壊す
    posts[1].dataset.imgs = 'not-base64!!';

    dom.window.imgClick(posts[0].querySelector('img'));
    let threw = false;
    try { press(dom, 'ArrowDown'); } catch (e) { threw = true; }
    check('不正な data-imgs で例外を投げない', !threw);
    check('移動せず元の画像のまま', shown(dom) === 'a0.jpg', shown(dom));
    check('ライトボックスは開いたまま', isOpen(dom));
}


// ---------------------------------------------------------------- [7]
console.log('\n[7] 動画の再生リンク');
{
    const dom = buildDom('feed');
    const d = dom.window.document;
    const posts = d.querySelectorAll('.tweet-media');
    const play = () => d.getElementById('lb-play');
    const vpost = posts[3];   // 2枚組で2枚目が動画

    dom.window.imgClick(vpost.querySelectorAll('img')[0]);
    check('画像のときは再生リンクを隠す', play().style.display === 'none',
          play().style.display);

    press(dom, 'ArrowRight');
    check('動画に移ると再生リンクが出る', play().style.display !== 'none',
          play().style.display);
    check('リンク先が投稿URL',
          play().getAttribute('href') === 'https://www.instagram.com/p/VID1/',
          play().getAttribute('href'));

    press(dom, 'ArrowLeft');
    check('画像に戻ると再生リンクが消える', play().style.display === 'none',
          play().style.display);

    // 動画を含まない投稿へ移動しても消えたまま
    press(dom, 'ArrowUp');
    check('data-vids が無い投稿では出ない', play().style.display === 'none',
          play().style.display);

    // 動画付き投稿へ ↓ で戻る（先頭画像なので出ない）
    press(dom, 'ArrowDown');
    check('↓ で戻ると先頭画像なので出ない', play().style.display === 'none');
    press(dom, 'ArrowRight');
    check('そこから → で再び出る', play().style.display !== 'none');
}

// ---------------------------------------------------------------- [8]
console.log('\n[8] data-vids が壊れていても落ちない');
{
    const dom = buildDom('feed');
    const d = dom.window.document;
    const posts = d.querySelectorAll('.tweet-media');
    posts[3].dataset.vids = 'broken!!';
    let threw = false;
    try { dom.window.imgClick(posts[3].querySelectorAll('img')[1]); }
    catch (e) { threw = true; }
    check('例外を投げない', !threw);
    check('画像は表示される', shown(dom) === 'v1.jpg', shown(dom));
    check('再生リンクは出さない',
          d.getElementById('lb-play').style.display === 'none');
}


// ---------------------------------------------------------------- [9]
console.log('\n[9] ミュート確認ダイアログ');
{
    const dom = buildDom('feed');
    const d = dom.window.document;
    const dlg = () => d.getElementById('mute-dlg');
    const isDlgOpen = () => dlg().classList.contains('open');

    check('初期状態では閉じている', !isDlgOpen());

    dom.window.openMuteDialog('userA');
    check('開くとダイアログが出る', isDlgOpen());
    check('対象アカウント名が入る',
          d.getElementById('mute-dlg-user').textContent === 'userA',
          d.getElementById('mute-dlg-user').textContent);

    // キャンセル
    dom.window.closeMuteDialog();
    check('キャンセルで閉じる', !isDlgOpen());
    check('カードは消えない',
          d.querySelectorAll('.tweet[data-owner="userA"]').length === 2);

    // Escape で閉じる
    dom.window.openMuteDialog('userA');
    press(dom, 'Escape');
    check('Escape で閉じる', !isDlgOpen());
}

(async () => {
// ---------------------------------------------------------------- [10]
console.log('\n[10] ミュート実行');
{
    const dom = buildDom('feed');
    const d = dom.window.document;
    const sent = [];

    dom.window.fetch = async (url, opts) => {
        sent.push({ url, body: JSON.parse(opts.body) });
        return {
            ok: true, status: 200,
            json: async () => ({ ok: true, muted: true, username: 'userA', changed: true })
        };
    };

    dom.window.openMuteDialog('userA');
    d.getElementById('mute-dlg-reason').value = 'うるさいので';
    await dom.window.muteDlgConfirm();

    check('APIを1回呼ぶ', sent.length === 1, String(sent.length));
    check('正しいエンドポイント',
          sent[0] && sent[0].url === '/api/mute/toggle', sent[0] && sent[0].url);
    check('username を送る', sent[0] && sent[0].body.username === 'userA');
    check('muted:true を明示する', sent[0] && sent[0].body.muted === true,
          JSON.stringify(sent[0] && sent[0].body));
    check('理由を送る', sent[0] && sent[0].body.reason === 'うるさいので');

    check('ダイアログが閉じる', !d.getElementById('mute-dlg').classList.contains('open'));
    check('対象アカウントのカードが消える',
          d.querySelectorAll('.tweet[data-owner="userA"]').length === 0);
    check('他のアカウントのカードは残る',
          d.querySelectorAll('.tweet[data-owner="userB"]').length === 1);

    const toast = d.getElementById('ir-toast');
    check('トーストが出る', toast && toast.classList.contains('show'));
    check('件数が入る', toast && toast.textContent.includes('2件'), toast && toast.textContent);
}

// ---------------------------------------------------------------- [11]
console.log('\n[11] ミュート失敗時');
{
    const dom = buildDom('feed');
    const d = dom.window.document;
    dom.window.fetch = async () => ({
        ok: false, status: 500, json: async () => ({ ok: false, error: 'DB error' })
    });

    dom.window.openMuteDialog('userA');
    await dom.window.muteDlgConfirm();

    check('失敗時はダイアログを閉じない',
          d.getElementById('mute-dlg').classList.contains('open'));
    check('エラーが表示される',
          d.getElementById('mute-dlg-msg').textContent.includes('DB error'),
          d.getElementById('mute-dlg-msg').textContent);
    check('カードは消えない',
          d.querySelectorAll('.tweet[data-owner="userA"]').length === 2);
    check('再試行できる', !d.getElementById('mute-dlg-ok').disabled);

    // 通信エラー
    dom.window.fetch = async () => { throw new Error('offline'); };
    await dom.window.muteDlgConfirm();
    check('通信エラーも表示する',
          d.getElementById('mute-dlg-msg').textContent.includes('通信エラー'),
          d.getElementById('mute-dlg-msg').textContent);
    check('カードは消えない（通信エラー）',
          d.querySelectorAll('.tweet[data-owner="userA"]').length === 2);
}

// ---------------------------------------------------------------- [12]
console.log('\n[12] 既にミュート済みだった場合');
{
    const dom = buildDom('feed');
    const d = dom.window.document;
    dom.window.fetch = async () => ({
        ok: true, status: 200,
        json: async () => ({ ok: true, muted: true, username: 'userA', changed: false })
    });

    dom.window.openMuteDialog('userA');
    await dom.window.muteDlgConfirm();
    const toast = d.getElementById('ir-toast');
    check('解除せず「既にミュート中」と伝える',
          toast && toast.textContent.includes('既にミュート中'),
          toast && toast.textContent);
    check('カードは消える（表示は揃える）',
          d.querySelectorAll('.tweet[data-owner="userA"]').length === 0);
}



// ---------------------------------------------------------------- [13]
console.log('\n[13] ギャラリーの行移動（上下キー）');
{
    const dom = buildDom('gallery');
    const d = dom.window.document;
    const items = [...d.querySelectorAll('.gallery-item')];

    // jsdom はレイアウトを持たないので、3列×2行の座標を自前で与える
    items.forEach((el, i) => {
        const top = Math.floor(i / 3) * 200;
        el.getBoundingClientRect = () => ({ top, left: (i % 3) * 100,
                                            right: 0, bottom: 0, width: 100, height: 200 });
    });

    check('列数を数えられる', dom.window.lbGridColumns() === 3,
          String(dom.window.lbGridColumns()));

    dom.window.imgClick(items[0].querySelector('img'));
    check('先頭を開く', shown(dom) === 'g0.jpg', shown(dom));

    press(dom, 'ArrowDown');
    check('↓ で1行下（3つ先）へ', shown(dom) === 'g3.jpg', shown(dom));

    press(dom, 'ArrowUp');
    check('↑ で1行上へ戻る', shown(dom) === 'g0.jpg', shown(dom));

    // 行が足りないときは端に寄せる
    dom.window.imgClick(items[2].querySelector('img'));
    press(dom, 'ArrowDown');
    check('2行目の同じ列へ', shown(dom) === 'g5.jpg', shown(dom));
    press(dom, 'ArrowDown');
    check('最終行で ↓ は末尾に留まる', shown(dom) === 'g5.jpg', shown(dom));

    dom.window.imgClick(items[1].querySelector('img'));
    press(dom, 'ArrowUp');
    check('1行目で ↑ は先頭へ寄せる', shown(dom) === 'g0.jpg', shown(dom));

    // 左右は1つずつ
    dom.window.imgClick(items[0].querySelector('img'));
    press(dom, 'ArrowRight');
    press(dom, 'ArrowRight');
    check('→ は1つずつ進む', shown(dom) === 'g2.jpg', shown(dom));
    press(dom, 'ArrowRight');
    check('行をまたいで進む', shown(dom) === 'g3.jpg', shown(dom));

    // 1列レイアウトでも壊れない
    items.forEach((el, i) => {
        el.getBoundingClientRect = () => ({ top: i * 200, left: 0,
                                            right: 0, bottom: 0, width: 100, height: 200 });
    });
    check('1列なら列数は1', dom.window.lbGridColumns() === 1,
          String(dom.window.lbGridColumns()));
    dom.window.imgClick(items[0].querySelector('img'));
    press(dom, 'ArrowDown');
    check('1列では ↓ が隣へ', shown(dom) === 'g1.jpg', shown(dom));
}

// ---------------------------------------------------------------- [14]
console.log('\n[14] フィードは従来通り（回帰）');
{
    const dom = buildDom('feed');
    const d = dom.window.document;
    const posts = d.querySelectorAll('.tweet-media');

    // 複数枚の投稿では左右は投稿内を回る
    dom.window.imgClick(posts[1].querySelectorAll('img')[0]);
    press(dom, 'ArrowRight');
    check('複数枚では投稿内を移動', shown(dom) === 'b1.jpg', shown(dom));

    // 単一画像の投稿では左右が隣の投稿へ
    dom.window.imgClick(posts[0].querySelector('img'));
    press(dom, 'ArrowRight');
    check('1枚の投稿では → が次の投稿へ', shown(dom) === 'b0.jpg', shown(dom));
}

console.log('\n' + '='.repeat(50));
console.log(`PASS ${PASS.length} / FAIL ${FAIL.length}`);
if (FAIL.length) { FAIL.forEach(f => console.log('  - ' + f)); process.exit(1); }
console.log('すべて通過');
})();
