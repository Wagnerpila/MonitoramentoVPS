# Monitor da VPS

Painel próprio com CPU, memória, disco e tráfego de rede da VPS, coletados direto do Linux.
Opcionalmente mostra também CPU, memória, rede e status de cada aplicativo do EasyPanel.
Sem banco de dados e sem dependências: Python puro + uma página HTML.

## Arquivos

- `app.py` – coleta as métricas a cada 5 s, guarda 1 hora de histórico em memória e serve a página e a API
- `index.html` – a página: mostradores, gráficos, lista de aplicativos e alertas, em 3 temas de cores
- `Dockerfile` – imagem para rodar no EasyPanel

## Temas de cores

As bolinhas no canto superior direito da página trocam o tema: **ciano** (verde-azulado, estilo HUD),
**neon** (magenta, violeta e laranja) e **ciber** (azul, violeta e rosa). A escolha fica salva no
navegador de quem abriu a página. A fonte (Barlow) vem do Google Fonts; se o navegador não conseguir
carregá-la, a página usa a fonte padrão do sistema e continua funcionando.

## Deploy no EasyPanel

1. Suba estes 4 arquivos para um repositório no GitHub (pode ser privado).
2. No EasyPanel: **Create Service → App**. Dê um nome, por exemplo `monitoramento`.
3. Em **Source**, escolha **GitHub**, selecione o repositório e o ramo `main`. Em **Build**, deixe **Dockerfile**.
4. Em **Environment**, adicione:
   ```
   DASH_USER=seu_usuario
   DASH_PASS=uma_senha_forte
   HOST_LABEL=Minha VPS
   ```
   Sem `DASH_USER` e `DASH_PASS` a página fica aberta para qualquer pessoa com o link.
5. Em **Mounts**, adicione um **Bind Mount**:
   - Caminho do Host: `/sys`
   - Caminho de Montagem: `/host/sys`

   Esse mount faz o painel enxergar a placa de rede da VPS, e não só a do container.
   CPU, memória, disco e uptime funcionam mesmo sem ele.
6. Em **Domains**, aponte um domínio ou subdomínio para a **porta 8080**. O EasyPanel cuida do HTTPS.
7. Clique em **Deploy**.

## Aplicativos do EasyPanel (opcional)

Cada app do EasyPanel é um container Docker. Para o painel listar CPU, memória, rede e status de
cada um, ele precisa conversar com o Docker da VPS. Sem isso, a tabela mostra um aviso e o resto
do painel funciona normalmente.

**Atenção:** acesso ao Docker é acesso sensível. Quem controla o socket do Docker controla o servidor
inteiro. Por isso a opção A, com proxy somente leitura, é a recomendada.

### Opção A (recomendada): proxy somente leitura

1. **Create Service → App**, nome `docker-proxy`, em **Source** escolha **Imagem Docker** e use
   `tecnativa/docker-socket-proxy`.
2. Em **Environment** do proxy, adicione só isto (todo o resto fica bloqueado por padrão):
   ```
   CONTAINERS=1
   ```
3. Em **Mounts** do proxy, adicione um **Bind Mount**:
   - Caminho do Host: `/var/run/docker.sock`
   - Caminho de Montagem: `/var/run/docker.sock`
4. **Não crie domínio** para o proxy. Ele só deve ser alcançado por dentro do EasyPanel.
5. No serviço do **monitor**, em **Environment**, adicione (o nome interno costuma ser
   `projeto_servico`; confirme na tela do serviço do proxy):
   ```
   DOCKER_HOST=tcp://evolution_docker-proxy:2375
   ```
6. Faça **Deploy** do proxy e depois do monitor.

### Opção B: socket direto no monitor

No serviço do monitor, adicione um **Bind Mount** de `/var/run/docker.sock` para
`/var/run/docker.sock`. É mais simples, mas, se alguém invadir o monitor, ganha controle total da VPS.
Se usar esta opção, mantenha `DASH_USER` e `DASH_PASS` bem fortes.

### Como ler a tabela

- Cada linha é um serviço do EasyPanel, no formato `projeto / serviço`. Containers fora do padrão
  (como o próprio `easypanel`) aparecem só com o nome.
- **CPU:** 100% equivale a 1 vCPU inteira (igual ao `docker stats`). Numa VPS de 2 vCPUs o máximo é 200%.
- **Memória:** mostra o uso real; o limite só aparece se o app tiver um limite de memória configurado.
- Serviços com várias réplicas aparecem somados (`×2`). Containers antigos e parados de um serviço
  que está rodando ficam escondidos.
- O mini gráfico mostra o CPU dos últimos ~10 minutos.

## Alertas por WhatsApp (opcional)

O monitor manda mensagem pela sua Evolution API quando algo passa do limite ou quando um app para.
Configure estas variáveis no **Environment** do serviço do monitor:

```
EVOLUTION_URL=http://evolution_evolution-api:8080
EVOLUTION_INSTANCE=nome-da-sua-instancia
EVOLUTION_APIKEY=a-chave-da-api
ALERT_NUMBER=5543999999999
```

- `EVOLUTION_URL`: endereço da Evolution API. Dentro do EasyPanel use o nome interno do serviço
  (`http://projeto_servico:porta`); se preferir, use o domínio público (`https://...`).
- `EVOLUTION_INSTANCE`: nome da instância conectada ao seu WhatsApp, como aparece no manager da Evolution.
- `EVOLUTION_APIKEY`: chave da API (global ou da instância). Guarde só aqui, nunca no GitHub.
- `ALERT_NUMBER`: número que recebe os avisos, com código do país e DDD, só números
  (ex.: `5543999999999`). Para vários números, separe por vírgula.

Depois do deploy, abra o painel, vá no quadro **Alertas por WhatsApp** e clique em
**Enviar mensagem de teste**. Se falhar, o painel mostra o motivo (variável faltando, chave errada etc.).

### O que gera aviso

| Situação | Limite padrão | Variável |
|---|---|---|
| CPU da VPS | 90% | `ALERT_CPU_PCT` |
| Memória da VPS | 90% | `ALERT_MEM_PCT` |
| Disco da VPS | 85% | `ALERT_DISK_PCT` |
| CPU de um app (100% = 1 vCPU) | 90% | `ALERT_APP_CPU_PCT` |
| Memória de um app (% do limite dele) | 90% | `ALERT_APP_MEM_PCT` |
| App parado | parado há 60 s | `ALERT_DOWN_SECONDS` |

Use `0` em qualquer limite para desligar aquele alerta. Os alertas de apps precisam do acesso ao Docker
(seção anterior); os da VPS funcionam sem ele.

### Como se comporta

- Os limites de uso só disparam se o problema durar `ALERT_SUSTAIN_SECONDS` (padrão 120 s), para
  picos rápidos não gerarem mensagem.
- Enquanto o problema continuar, o monitor lembra a cada `ALERT_COOLDOWN_MINUTES` (padrão 30 min).
- Quando volta ao normal, manda uma mensagem de "Normalizado".
- O alerta de "app parado" só vale para apps que o monitor já viu rodando. Serviços que você parou
  de propósito antes não geram aviso. Se você redeploya um app e ele leva mais de 60 s para voltar,
  aumente `ALERT_DOWN_SECONDS`.
- As horas nas mensagens usam `TZ_OFFSET_HOURS` (padrão `-3`, horário de Brasília).

### Limitação importante

Se a **VPS inteira** cair, o monitor cai junto e não consegue avisar. Para esse caso, use também um
verificador externo (por exemplo UptimeRobot ou Better Stack) apontando para o domínio do painel.

## Variáveis opcionais

| Variável | Padrão | Para quê |
|---|---|---|
| `SAMPLE_INTERVAL` | `5` | segundos entre coletas |
| `HISTORY_POINTS` | `720` | pontos guardados (720 × 5 s = 1 h) |
| `DISK_PATH` | `/` | caminho cujo uso de disco é medido |
| `PORT` | `8080` | porta interna |
| `DOCKER_HOST` | vazio | `tcp://host:2375` (proxy) ou `unix:///var/run/docker.sock`; vazio desativa a tabela, a não ser que o socket esteja montado |
| `ALERT_SUSTAIN_SECONDS` | `120` | quanto tempo um limite de uso precisa durar para avisar |
| `ALERT_COOLDOWN_MINUTES` | `30` | intervalo entre lembretes |
| `TZ_OFFSET_HOURS` | `-3` | fuso usado na hora das mensagens |

## Observações

- O histórico fica na memória: ao reiniciar o container, os gráficos recomeçam do zero.
- Os totais de rede (recebido/enviado) contam desde o último boot da VPS, não do mês.
- Para testar localmente: `python app.py` e abra http://localhost:8080
