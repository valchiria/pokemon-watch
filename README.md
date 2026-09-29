# Robot Pokémon

Ogni ora cerca i set elencati in `config.json` in tutti i negozi della lista, usando la
ricerca ufficiale di ogni negozio, e scrive su Telegram:

- 📅 **Preordine aperto**: un messaggio per prodotto con *tutti* i negozi dove è prenotabile,
  ordinati per prezzo con spedizione, più quelli con la scheda online ma non ancora prenotabile.
- 👀 **In arrivo**: è comparsa la scheda di un prodotto nuovo, non ancora prenotabile.
- ⏰ **Promemoria** 6 settimane, 1 settimana e 1 giorno prima di ogni uscita del calendario.
- 🟢 **Di nuovo disponibile**, 🆕 **Appena comparso**, 💶 **Prezzo sceso**, con spedizione,
  prezzo per busta, minimo mai visto e valore medio delle carte.
- ☀️ Ogni mattina dalle 9 il **riepilogo**: prossime uscite, preordini aperti, migliori offerte.
  Il lunedì anche il **valore delle carte** di ogni set (prezzi Cardmarket via TCGdex) e le carte top.

## Il "cervello" da collezionista
- **Indice di interesse (0-100)** per ogni prodotto: tipo (Set Allenatore di set speciali, Ultra Premium,
  esclusive), negozi esauriti, ricarico sul listino, disponibilità in calo. Con interesse alto il
  preordine arriva come 🔥 PRIORITÀ e il prodotto viene segnalato anche un po' sopra listino.
- **Set nuovi da soli**: legge pokemon.com/it (ufficiale), Pokémon Millennium e i titoli dei negozi.
  Un set nuovo viene seguito solo se confermato dalla fonte ufficiale, oppure da Pokémon Millennium
  + un negozio, oppure da 3 negozi con preordini/data futura. Le date ufficiali correggono il calendario.
- **Set raffreddati archiviati da soli**: dopo 3 settimane in cui si trovano ovunque a listino e le
  carte top calano, niente più avvisi (restano nel cruscotto). Se tornano caldi si riattivano.

## Cruscotto
- La mattina arriva un'**immagine** con il riepilogo e i pulsanti delle 3 cose da guardare.
- La pagina completa, aggiornata ogni ora, è su GitHub Pages (`cruscotto_url` in config.json):
  va attivata una volta in *Settings → Pages → Deploy from a branch → main / docs*.

## Giudizio sul prezzo
Ogni prezzo è confrontato con un **riferimento**: il listino ufficiale (`listini` dentro il set), per
box/set allenatore/bundle/blister/Ultra Premium il listino tipico (`soglie`), per collezioni e tin il
prezzo con cui i negozi l'hanno lanciato. Fasce:
- 🟢 **sotto listino** (almeno -5%) · ⚪ **in linea** (±5%) · 🟡 **accettabile** (fino a +15%, +30% per i
  prodotti molto richiesti: `tolleranza`) · 🔴 **gonfiato** (oltre).
- I prezzi gonfiati non generano avvisi né pulsanti: il robot indica il **prezzo obiettivo** e manda
  🎯 **PREZZO OBIETTIVO RAGGIUNTO** quando un negozio scende sotto.
- Eccezione: prodotti con buste il cui valore medio in carte è almeno `affare_buste` × il prezzo.

## Spedizione e carrelli
- Il giudizio si calcola sul **costo consegnato** (prezzo + spedizione): un blister a 15 € con 9 € di
  spedizione costa 24 € e non è un affare da solo.
- Il costo di spedizione di ogni negozio viene **letto dal carrello** una volta a settimana (un prodotto
  economico nel carrello, indirizzo di Milano, nessun ordine). Se non si riesce, vale `spedizione` in
  config.json, altrimenti una stima di 6,90 € (segnata con ~).
- 🧺 **Carrello consigliato**: se nello stesso negozio ci sono altre occasioni vere (sotto listino o in
  linea), il robot propone la combinazione più piccola che arriva alla spedizione gratuita.

## Cose da sapere
- Il valore delle carte è una **stima**: probabilità di uscita della community (non ufficiali),
  prezzi Cardmarket di tendenza, buste reverse non contate. Per i set speciali (buste diverse)
  non viene calcolato.
- Il prezzo di mercato dei prodotti **sigillati** non c'è: nessuna fonte gratuita lo pubblica.
  Come riferimento il robot usa il listino e il minimo mai visto nei negozi.
- Le date con `"stima": true` nel calendario vengono dall'uscita inglese: da confermare.

## Modifiche frequenti
- Seguire una nuova espansione: aggiungila in `set` con nome italiano, `cerca`, `en` (nome
  inglese, per il valore delle carte), `uscita` e `certo`.
- Aggiungere un'uscita a ondate o una data da ricordare: aggiungila in `calendario`.
- Aggiungere un negozio: aggiungilo in `negozi` (tipo `shopify` o `woo`) con la spedizione.
- Lanciarlo subito: scheda **Actions** → *Controllo Pokémon* → **Run workflow**.

Il giro orario parte da cron-job.org (che chiama *Run workflow* con `riepilogo: false`);
il timer di GitHub resta come riserva. Test: `python tests/test_watch.py`.
