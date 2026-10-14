# Monitor da VPS

Painel próprio com CPU, memória, disco e tráfego de rede da VPS, coletados direto do Linux.
Opcionalmente mostra também CPU, memória, rede e status de cada aplicativo do EasyPanel.
Sem banco de dados e sem dependências: Python puro + uma página HTML.

## Arquivos

- `app.py` – coleta as métricas a cada 5 s, guarda 1 hora de histórico em memória e serve a página e a API
- `index.html` – a página com os gráficos e a tabela de aplicativos
- `Dockerfile` – imagem para rodar no EasyPanel

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

## Variáveis opcionais

| Variável | Padrão | Para quê |
|---|---|---|
| `SAMPLE_INTERVAL` | `5` | segundos entre coletas |
| `HISTORY_POINTS` | `720` | pontos guardados (720 × 5 s = 1 h) |
| `DISK_PATH` | `/` | caminho cujo uso de disco é medido |
| `PORT` | `8080` | porta interna |
| `DOCKER_HOST` | vazio | `tcp://host:2375` (proxy) ou `unix:///var/run/docker.sock`; vazio desativa a tabela, a não ser que o socket esteja montado |

## Observações

- O histórico fica na memória: ao reiniciar o container, os gráficos recomeçam do zero.
- Os totais de rede (recebido/enviado) contam desde o último boot da VPS, não do mês.
- Para testar localmente: `python app.py` e abra http://localhost:8080
