"""Gera instalar_favorito.html: junta nucleo.js + leitor.js + favorito.js num único favorito (bookmarklet).

Rode depois de alterar qualquer um desses arquivos: python favorito/gerar.py
"""
import html
import re
from pathlib import Path
from urllib.parse import quote

AQUI = Path(__file__).resolve().parent
EXT = AQUI.parent / "extensao_chrome"


def montar_codigo() -> str:
    nucleo = re.sub(r"^export ", "", (EXT / "nucleo.js").read_text(encoding="utf-8"), flags=re.M)
    leitor = (EXT / "leitor.js").read_text(encoding="utf-8")
    favorito = (AQUI / "favorito.js").read_text(encoding="utf-8")
    return f"(()=>{{\n{nucleo}\n{leitor}\n{favorito}\n}})();void 0;"


def montar_url(codigo: str) -> str:
    return "javascript:" + quote(codigo, safe="")


PAGINA = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>Instalar o favorito do Monitor de Rotas Extras</title>
<style>
body {{ font-family: "Segoe UI", Arial, sans-serif; max-width: 760px; margin: 32px auto; padding: 0 16px; color: #1f2328; line-height: 1.5; }}
a.favorito {{ display: inline-block; padding: 10px 18px; background: #0b3d91; color: #fff; border-radius: 8px;
  text-decoration: none; font-weight: 600; font-size: 15px; }}
textarea {{ width: 100%; height: 90px; font-size: 11px; }}
code {{ background: #f0f2f4; padding: 1px 4px; border-radius: 4px; }}
</style></head><body>
<h1>Monitor de Rotas Extras (favorito)</h1>
<p>Roda na aba do AppSheet em que você já está logado. Não precisa de Python, extensão nem login novo.
Só lê a tela: nunca clica em ACEITAR nem em recusar.</p>

<h2>1. Instalar (uma vez)</h2>
<p>Mostre a barra de favoritos (<code>Ctrl+Shift+B</code>) e <b>arraste</b> este botão para ela:</p>
<p><a class="favorito" href="{url}">🚚 Monitor Rotas Extras</a></p>
<p>Se arrastar não funcionar: copie todo o texto abaixo, abra o gerenciador de favoritos (<code>Ctrl+Shift+O</code>),
"Adicionar novo favorito", nome <code>Monitor Rotas Extras</code> e cole o texto no campo URL.</p>
<textarea readonly onclick="this.select()">{url_texto}</textarea>

<h2>2. Usar</h2>
<ol>
<li>Abra o app e vá em MENU &gt; ROTA EXTRA. Use essa aba só para o monitor (trabalhe em outras abas).</li>
<li>Clique no favorito <b>Monitor Rotas Extras</b>. Aparece uma barra azul por cima do app.</li>
<li>Na primeira vez, clique em "Configurar", preencha pelo menos um canal (abaixo), clique em Salvar e depois em "Testar envio".</li>
<li>Pronto: a cada 60 s o app é recarregado dentro da barra e as rotas novas são avisadas em todos os canais configurados.</li>
</ol>

<h2>Canais de aviso (use um ou mais)</h2>
<ul>
<li><b>WhatsApp (CallMeBot)</b>: cada pessoa que vai receber ativa o próprio número uma vez. Siga as instruções
em <code>callmebot.com</code> (seção WhatsApp): adicionar o número do CallMeBot nos contatos e mandar a mensagem de
autorização que o site indica. O bot responde com uma <code>apikey</code>. Em "Configurar", coloque uma linha por
pessoa: <code>+5511999998888,123456</code> (telefone com DDI e DDD, vírgula, apikey). É um serviço gratuito de
terceiros, sem garantia de entrega; não manda para grupos.</li>
<li><b>Telegram</b>: no Telegram, fale com <code>@BotFather</code>, envie <code>/newbot</code> e guarde o token.
Adicione o bot ao grupo do time e mande qualquer mensagem no grupo. Para descobrir o chat id do grupo, abra
<code>https://api.telegram.org/bot&lt;TOKEN&gt;/getUpdates</code> e procure <code>"chat":{{"id":-100...}}</code>.
Funciona com grupos.</li>
<li><b>Teams</b>: URL de webhook do Workflows (se a política da empresa permitir).</li>
<li><b>E-mail</b>: enviado pela sua conta Google, através de um Google Apps Script. Passo a passo abaixo.</li>
</ul>

<h2>E-mail pelo Google Apps Script (uma vez)</h2>
<p>As rotas novas entram numa fila no Google e saem num <b>único e-mail</b> a cada 15 minutos (ajustável). O mesmo
script avisa por e-mail se o monitor parar de ler a tela (notebook desligado, aba fechada, sessão expirada).</p>
<ol>
<li>Logado na conta Google que vai <b>enviar</b> os e-mails, abra <code>https://script.google.com</code> e o projeto
(ou crie um "Novo projeto" chamado <code>Monitor Rotas Extras</code>).</li>
<li>Apague o que estiver no editor, cole todo o código abaixo e salve (Ctrl+S).
<textarea readonly onclick="this.select()" style="height:180px">{codigo_gs}</textarea></li>
<li>Engrenagem "Configurações do projeto" &gt; "Propriedades do script":
<code>TOKEN</code> = uma senha longa; <code>DESTINATARIOS</code> = e-mails separados por vírgula.
Opcionais: <code>INTERVALO_MIN</code> = 5, 10, 15 ou 30 (padrão 15); <code>LIMITE_SEM_SINAL_MIN</code> = minutos sem
sinal para avisar que o monitor parou (padrão 20).</li>
<li>No editor, escolha a função <code>instalarGatilhos</code> no topo e clique em "Executar". Autorize o acesso
(se aparecer "app não verificado": Avançado, Acessar). Isso cria o envio automático a cada 15 minutos.
Se mudar <code>INTERVALO_MIN</code> depois, rode <code>instalarGatilhos</code> de novo.</li>
<li>Publique: <b>primeira vez</b>: "Implantar", "Nova implantação", tipo "App da Web", "Executar como: Eu",
"Quem pode acessar: Qualquer pessoa", Implantar e copie a URL que termina em <code>/exec</code>.
<b>Já tinha implantado</b>: "Implantar", "Gerenciar implantações", lápis, "Versão: Nova versão", Implantar
(a URL continua a mesma).</li>
<li>No favorito, em "Configurar": cole a URL e o mesmo TOKEN, salve e clique em "Testar envio" (o teste sai na hora,
fora da fila).</li>
</ol>
<p>Observações: o e-mail só sai quando há rota nova ou aviso; com fila vazia, nada é enviado. Conta Gmail comum envia
até cerca de 100 destinatários por dia (um e-mail para 3 pessoas conta 3). Abrir a URL <code>/exec</code> no navegador
mostra quantas rotas estão na fila e o horário do último sinal do monitor. Com intervalo de 15 minutos, uma rota pode
chegar com metade do tempo de oferta já passado; se isso estiver fazendo o time perder rotas, use 5 ou 10. E-mails
vindos de conta externa podem cair no spam/quarentena do e-mail corporativo.</p>

<h2>3. Cuidados</h2>
<ul>
<li>Se a aba for recarregada ou fechada, o monitor para. Abra de novo a tela ROTA EXTRA e clique no favorito
(as rotas já avisadas ficam guardadas e não são repetidas).</li>
<li>Para o Chrome não "congelar" a aba: em <code>chrome://settings/performance</code>, adicione
<code>appsheet.com</code> em "Sempre manter estes sites ativos". Deixe a aba numa janela própria, sem minimizar.</li>
<li>O PC e o Chrome precisam ficar ligados. Se o computador suspender, o monitor para.</li>
<li>"Ativar alerta no PC" mostra também uma notificação do Windows (se o Chrome permitir).</li>
<li>As configurações (inclusive apikeys e token) ficam salvas só neste navegador, no armazenamento do site do
AppSheet. Use um bot do Telegram exclusivo para isso.</li>
<li>Os textos das rotas saem da rede da empresa para o WhatsApp/Telegram. Confirme com o gestor que isso é permitido.</li>
</ul>
</body></html>
"""


def main() -> None:
    url = montar_url(montar_codigo())
    codigo_gs = (AQUI / "apps_script" / "Codigo.gs").read_text(encoding="utf-8")
    destino = AQUI / "instalar_favorito.html"
    destino.write_text(PAGINA.format(url=html.escape(url, quote=True), url_texto=html.escape(url),
                                     codigo_gs=html.escape(codigo_gs)), encoding="utf-8")
    print(f"Gerado {destino} ({len(url) // 1024} KB)")


if __name__ == "__main__":
    main()
