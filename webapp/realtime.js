'use strict';
/*
 * Temps réel GTFS-RT et calcul des passages, côté téléphone.
 *
 * La PWA n'a plus de serveur : timetable.json (régénéré chaque nuit par
 * `nexttraintosee export`) donne pour chaque passage l'horaire en gare
 * d'appui et le décalage jusqu'au point d'observation. Il ne reste qu'à y
 * ajouter le retard, lu ici directement sur le flux SNCF.
 *
 * Le décodage protobuf est écrit à la main : on ne lit qu'une poignée de
 * champs de `FeedMessage`, et une bibliothèque générique pèserait plus lourd
 * que tout le reste de l'app. Numéros de champs : spécification GTFS-RT,
 * https://gtfs.org/realtime/reference/
 *
 * Miroir de `realtime.py` (resolve_delay, canceled_trip_ids) et de
 * `predict_passages` : toute divergence de règle entre les deux serait une
 * divergence d'heure annoncée.
 */

(function (root) {
  const TRIP_CANCELED = 3;  // TripDescriptor.ScheduleRelationship.CANCELED
  const STOP_SKIPPED = 1;   // StopTimeUpdate.ScheduleRelationship.SKIPPED

  // -------------------------------------------------------------------------
  // Lecture protobuf minimale
  // -------------------------------------------------------------------------

  /** Lecteur séquentiel d'un message protobuf, borné à [pos, end). */
  class Reader {
    constructor(bytes, pos = 0, end = bytes.length) {
      this.bytes = bytes;
      this.pos = pos;
      this.end = end;
    }

    done() { return this.pos >= this.end; }

    /** Varint tronqué à 32 bits signés : suffit pour un `int32` (retard,
     *  éventuellement négatif, encodé sur 10 octets), une énumération, une
     *  longueur, ou un horodatage Unix d'ici 2038. Les octets au-delà du
     *  32e bit sont consommés sans être lus. */
    varint() {
      let result = 0;
      let shift = 0;
      let byte;
      do {
        byte = this.bytes[this.pos++];
        if (shift < 32) result |= (byte & 0x7f) << shift;
        shift += 7;
      } while (byte & 0x80);
      return result;
    }

    /** Clé d'un champ : numéro et type de codage. */
    tag() {
      const key = this.varint() >>> 0;
      return { field: key >>> 3, wire: key & 7 };
    }

    /** Sous-lecteur sur un champ délimité (message, chaîne). */
    sub() {
      const length = this.varint() >>> 0;
      const reader = new Reader(this.bytes, this.pos, this.pos + length);
      this.pos += length;
      return reader;
    }

    string() {
      const reader = this.sub();
      return new TextDecoder().decode(this.bytes.subarray(reader.pos, reader.end));
    }

    skip(wire) {
      switch (wire) {
        case 0: this.varint(); break;
        case 1: this.pos += 8; break;
        // En deux temps : `this.pos += this.varint()` lirait `pos` *avant*
        // que varint() ne l'avance, et perdrait les octets de la longueur.
        case 2: { const length = this.varint() >>> 0; this.pos += length; break; }
        case 5: this.pos += 4; break;
        default: throw new Error(`type protobuf ${wire} non pris en charge`);
      }
    }
  }

  // -------------------------------------------------------------------------
  // FeedMessage → { timestamp, updates: Map<trip_id, TripUpdate> }
  // -------------------------------------------------------------------------

  /** StopTimeEvent : seul le retard (champ 1) nous intéresse. */
  function readEventDelay(reader) {
    let delay = null;
    while (!reader.done()) {
      const { field, wire } = reader.tag();
      if (field === 1 && wire === 0) delay = reader.varint();
      else reader.skip(wire);
    }
    return delay;
  }

  function readStopTimeUpdate(reader) {
    const update = { stopId: '', arrivalDelay: null, departureDelay: null, relationship: 0 };
    while (!reader.done()) {
      const { field, wire } = reader.tag();
      if (field === 2 && wire === 2) update.arrivalDelay = readEventDelay(reader.sub());
      else if (field === 3 && wire === 2) update.departureDelay = readEventDelay(reader.sub());
      else if (field === 4 && wire === 2) update.stopId = reader.string();
      else if (field === 5 && wire === 0) update.relationship = reader.varint();
      else reader.skip(wire);
    }
    return update;
  }

  function readTripDescriptor(reader, update) {
    while (!reader.done()) {
      const { field, wire } = reader.tag();
      if (field === 1 && wire === 2) update.tripId = reader.string();
      else if (field === 4 && wire === 0) update.relationship = reader.varint();
      else reader.skip(wire);
    }
  }

  function readTripUpdate(reader) {
    const update = { tripId: '', relationship: 0, stops: [] };
    while (!reader.done()) {
      const { field, wire } = reader.tag();
      if (field === 1 && wire === 2) readTripDescriptor(reader.sub(), update);
      else if (field === 2 && wire === 2) update.stops.push(readStopTimeUpdate(reader.sub()));
      else reader.skip(wire);
    }
    return update;
  }

  function readHeaderTimestamp(reader) {
    let timestamp = null;
    while (!reader.done()) {
      const { field, wire } = reader.tag();
      if (field === 3 && wire === 0) timestamp = reader.varint() >>> 0;
      else reader.skip(wire);
    }
    return timestamp;
  }

  /** Décode un FeedMessage GTFS-RT (Uint8Array) en mises à jour par circulation. */
  function decodeFeed(bytes) {
    const reader = new Reader(bytes);
    const updates = new Map();
    let timestamp = null;
    while (!reader.done()) {
      const { field, wire } = reader.tag();
      if (field === 1 && wire === 2) {
        timestamp = readHeaderTimestamp(reader.sub());
      } else if (field === 2 && wire === 2) {
        const entity = reader.sub();
        while (!entity.done()) {
          const t = entity.tag();
          if (t.field === 3 && t.wire === 2) {
            const update = readTripUpdate(entity.sub());
            if (update.tripId) updates.set(update.tripId, update);
          } else {
            entity.skip(t.wire);
          }
        }
      } else {
        reader.skip(wire);
      }
    }
    return { timestamp, updates };
  }

  // -------------------------------------------------------------------------
  // Retards et suppressions au droit de la gare d'appui
  // -------------------------------------------------------------------------

  /** Retard d'une circulation à la gare d'appui, ou null si inconnu.
   *
   *  GTFS-RT n'oblige pas à publier chaque arrêt : un retard se propage aux
   *  arrêts suivants jusqu'à la prochaine mise à jour. Le retard au départ,
   *  qui gouverne la sortie de gare, est préféré à celui à l'arrivée. */
  function resolveDelay(update, stopIds) {
    let propagated = null;
    for (const stop of update.stops) {
      const current = stop.departureDelay ?? stop.arrivalDelay;
      if (stopIds.has(stop.stopId)) return current ?? propagated;
      if (current !== null) propagated = current;
    }
    return null;
  }

  /** Retards (Map trip_id → s) et suppressions (Set) à la gare d'appui. */
  function delaysAt(feed, anchorStopIds) {
    const stopIds = new Set(anchorStopIds);
    const delays = new Map();
    const canceled = new Set();
    for (const [tripId, update] of feed.updates) {
      const delay = resolveDelay(update, stopIds);
      if (delay !== null) delays.set(tripId, delay);
      const skipped = update.stops.some((s) => s.relationship === STOP_SKIPPED && stopIds.has(s.stopId));
      if (update.relationship === TRIP_CANCELED || skipped) canceled.add(tripId);
    }
    return { delays, canceled };
  }

  // -------------------------------------------------------------------------
  // Passages : horaire en gare + retard + décalage jusqu'au point
  // -------------------------------------------------------------------------

  /** Passages à venir dans l'horizon, au format qu'affiche app.js.
   *
   *  @param {object} timetable  contenu de timetable.json
   *  @param {{delays: Map, canceled: Set} | null} realtime  null si indisponible
   *  @param {number} now  instant courant, en ms
   */
  function upcomingPassages(timetable, realtime, now, { horizonMs, limit }) {
    const delays = realtime ? realtime.delays : new Map();
    const canceled = realtime ? realtime.canceled : new Set();
    const result = [];
    for (const entry of timetable.passages) {
      if (canceled.has(entry.trip_id)) continue;
      const delay = delays.has(entry.trip_id) ? delays.get(entry.trip_id) : null;
      const when = (entry.anchor + (delay ?? 0) + entry.offset_s) * 1000;
      if (when < now || when > now + horizonMs) continue;
      const announce = when - (entry.uncertainty_s + entry.lead_margin_s) * 1000;
      result.push({
        ...entry,
        when: new Date(when).toISOString(),
        announce_at: new Date(announce).toISOString(),
        delay_s: delay,
        realtime: delay !== null,
      });
    }
    result.sort((a, b) => Date.parse(a.when) - Date.parse(b.when));
    return result.slice(0, limit);
  }

  const api = { decodeFeed, resolveDelay, delaysAt, upcomingPassages };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.NextTrainRealtime = api;
})(typeof self !== 'undefined' ? self : this);
