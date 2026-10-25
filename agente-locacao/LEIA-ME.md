# Agente de WhatsApp para locação de brinquedos

Um assistente de IA para o WhatsApp de locadoras de brinquedos infláveis, com clima de festa e
**um perfil por empresa**. A primeira empresa é a **Anima Kids Park** (Ribeirão Preto/SP), e o
agente se chama **Anima Kids Park Bot**.

## A regra de ouro: a IA atende, a equipe fecha

A IA:
- recebe o cliente no clima de festa e tira dúvidas (montagem, monitor, espaço, chuva, água);
- sugere os infláveis certos para a idade, o espaço e o tema, e manda a foto real;
- anota a ficha do evento (data, horário, local, crianças, idades, espaço, piso, tomada...);
- passa para a equipe quando a ficha fica pronta, ou quando o cliente pede preço, desconto,
  uma pessoa, desconfia de golpe ou pede algo "sob consulta" (pipoca, touro, jogos...).

A IA **nunca**:
- confirma data, reserva ou valor;
- dá endereço (a Anima Kids não tem loja: tudo é pelo WhatsApp);
- pede pagamento.

Essas travas estão no código: se a IA tentar escrever "sua data está confirmada" ou "R$ 300",
a mensagem não sai.

Quando passa para a equipe:
1. O cliente recebe uma mensagem fixa: "já passei para a nossa equipe...".
2. A equipe recebe a **ficha** no WhatsApp (números em `EQUIPE_WHATSAPP`) e no **painel**.
3. A IA fica quieta com aquele cliente. Ela volta a responder quando:
   - passam `PAUSA_HORAS` horas (padrão 12);
   - alguém clica em "Devolver para o agente" no painel;
   - alguém da equipe manda `#bot voltar 5516999998888` para o número da empresa.

Na Zernio, se alguém da equipe responder o cliente pelo celular, a IA percebe e também fica quieta.

## Como testar no computador

```bash
cd agente-locacao
pip install -r requirements.txt
cp .env.example .env          # coloque a ANTHROPIC_API_KEY
python tests/test_local.py    # conversa no terminal, como se fosse o WhatsApp
```

No terminal dá para escrever `ficha` (mostra o que a IA anotou) e `limpar` (recomeça).

Outros testes:

```bash
pip install pytest pytest-asyncio
pytest                         # 37 testes, sem gastar API (usam um Claude falso)
python tests/rodar_conversas.py   # 12 cenários reais com a API de verdade
```

## Como mudar o jeito do agente (sem mexer no código)

Tudo da empresa fica em `empresas/anima-kids/`:

| Arquivo | O que tem |
|---|---|
| `perfil.yaml` | nome do agente, tom, emojis, frases da marca, mensagem de passagem para a equipe |
| `catalogo.yaml` | brinquedos: idade, medidas, se é aquático, frase de destaque, fotos, preço |
| `regras.yaml` | mostrar preço ou não, endereço, Pix, aviso contra golpe, perguntas do orçamento |
| `faq.md` | perguntas frequentes |

**Para cadastrar outra empresa:** copie `empresas/_modelo/` para `empresas/<nome>/`, preencha e
mude `EMPRESA=<nome>` no `.env`. Cada empresa vira um deploy próprio.

**Quando o dono passar os preços:** preencha `preco` no catálogo e mude `mostrar_precos: true`
em `regras.yaml`. Valor fechado, frete e desconto continuam com a equipe.

## Colocar no ar (Railway + Zernio)

Segue o mesmo caminho do AgentKit (ver o `CLAUDE.md` da raiz, Fase 5). Resumo:

1. **Railway:** New Project → Deploy from GitHub → escolha o repositório e, em Settings,
   defina o **Root Directory** como `agente-locacao`.
2. **Banco:** New → Database → PostgreSQL, e nas variáveis do agente coloque
   `DATABASE_URL=${{Postgres.DATABASE_URL}}`. Sem isso, a cada deploy o agente esquece os clientes.
3. **Variáveis:** as do `.env.example`, com `ENVIRONMENT=production` e **sem** `PORT`.
4. **Domínio:** Settings → Networking → Generate Domain. Confira com `curl https://seu-app.up.railway.app/`.
5. **Zernio:** Webhooks → Create webhook → URL `https://seu-app.up.railway.app/webhook`,
   o mesmo segredo de `ZERNIO_WEBHOOK_SECRET`, eventos **message.received** e **message.sent**
   (o segundo é o que avisa quando a equipe responde pelo celular).
6. **Painel da equipe:** `https://seu-app.up.railway.app/painel?token=SUA_SENHA`.

## Como o código está organizado

```
agent/
  main.py       servidor, webhook, passagem para a equipe, comandos #bot, painel
  brain.py      conversa com o Claude: ciclo de ferramentas + travas
  prompt.py     monta as instruções da IA a partir de empresas/<slug>/
  tools.py      as 5 ferramentas e a ficha da equipe
  empresa.py    lê e confere os arquivos da empresa
  memory.py     histórico, ficha, pausa e deduplicação (SQLite / PostgreSQL)
  providers/    Zernio e Meta (texto e foto)
empresas/       um perfil por empresa (+ _modelo/)
tests/          testes sem API, conversa no terminal, bateria de cenários
```

## O que ainda falta (quando o dono passar os dados)

Preços e combos, frete, sinal e Pix oficial, horário de atendimento, número da equipe,
estoque/agenda e conversas reais para afinar ainda mais o tom.
