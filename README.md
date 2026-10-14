# Monitor da VPS

Painel próprio com CPU, memória, disco e tráfego de rede da VPS, coletados direto do Linux.
Sem banco de dados e sem dependências: Python puro + uma página HTML.

## Arquivos

- `app.py` – coleta as métricas a cada 5 s, guarda 1 hora de histórico em memória e serve a página e a API
- `index.html` – a página com os gráficos
- `Dockerfile` – imagem para rodar no EasyPanel

## Deploy no EasyPanel

1. Suba estes 4 arquivos para um repositório no GitHub (pode ser privado).
2. No EasyPanel: **Create Service → App**. Dê um nome, por exemplo `monitor`.
3. Em **Source**, escolha **GitHub**, selecione o repositório e a branch. Em **Build**, deixe **Dockerfile**.
4. Em **Environment**, adicione:
   ```
   DASH_USER=seu_usuario
   DASH_PASS=uma_senha_forte
   HOST_LABEL=Minha VPS
   ```
   Sem `DASH_USER` e `DASH_PASS` a página fica aberta para qualquer pessoa com o link.
5. Em **Mounts**, adicione um **Bind Mount**:
   - Host path: `/sys`
   - Mount path: `/host/sys`
   - Marque como somente leitura, se houver a opção.

   Esse mount faz o painel enxergar a placa de rede da VPS, e não só a do container.
   CPU, memória, disco e uptime funcionam mesmo sem ele.
6. Em **Domains**, aponte um domínio ou subdomínio para a **porta 8080**. O EasyPanel cuida do HTTPS.
7. Clique em **Deploy**.

## Variáveis opcionais

| Variável | Padrão | Para quê |
|---|---|---|
| `SAMPLE_INTERVAL` | `5` | segundos entre coletas |
| `HISTORY_POINTS` | `720` | pontos guardados (720 × 5 s = 1 h) |
| `DISK_PATH` | `/` | caminho cujo uso de disco é medido |
| `PORT` | `8080` | porta interna |

## Observações

- O histórico fica na memória: ao reiniciar o container, os gráficos recomeçam do zero.
- Os totais de rede (recebido/enviado) contam desde o último boot da VPS, não do mês.
- Para testar localmente: `python app.py` e abra http://localhost:8080
