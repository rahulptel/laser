import hashlib
import json
import multiprocessing as mp
import time

import hydra
import libbddenvv1
import networkx as nx
import numpy as np
import pandas as pd
import torch
from torchmetrics.classification import BinaryStatScores

from laser import resource_path
from laser.utils import get_instance_data
from laser.utils import get_order
from laser.utils import get_xgb_model_name
from laser.utils import label_bdd
from laser.utils import statscore
import gurobipy as gp


def call_get_model_name(cfg):
    return get_xgb_model_name(max_depth=cfg.max_depth,
                              eta=cfg.eta,
                              min_child_weight=cfg.min_child_weight,
                              subsample=cfg.subsample,
                              colsample_bytree=cfg.colsample_bytree,
                              objective=cfg.objective,
                              num_round=cfg.num_round,
                              early_stopping_rounds=cfg.early_stopping_rounds,
                              evals=cfg.evals,
                              eval_metric=cfg.eval_metric,
                              seed=cfg.seed,
                              prob_name=cfg.prob.name,
                              num_objs=cfg.prob.num_objs,
                              num_vars=cfg.prob.num_vars,
                              order=cfg.prob.order,
                              layer_norm_const=cfg.prob.layer_norm_const,
                              state_norm_const=cfg.prob.state_norm_const,
                              train_from_pid=cfg.train.from_pid,
                              train_to_pid=cfg.train.to_pid,
                              train_neg_pos_ratio=cfg.train.neg_pos_ratio,
                              train_min_samples=cfg.train.min_samples,
                              train_flag_layer_penalty=cfg.train.flag_layer_penalty,
                              train_layer_penalty=cfg.train.layer_penalty,
                              train_flag_imbalance_penalty=cfg.train.flag_imbalance_penalty,
                              train_flag_importance_penalty=cfg.train.flag_importance_penalty,
                              train_penalty_aggregation=cfg.train.penalty_aggregation,
                              val_from_pid=cfg.val.from_pid,
                              val_to_pid=cfg.val.to_pid,
                              val_neg_pos_ratio=cfg.val.neg_pos_ratio,
                              val_min_samples=cfg.val.min_samples,
                              val_flag_layer_penalty=cfg.val.flag_layer_penalty,
                              val_layer_penalty=cfg.val.layer_penalty,
                              val_flag_imbalance_penalty=cfg.val.flag_imbalance_penalty,
                              val_flag_importance_penalty=cfg.val.flag_importance_penalty,
                              val_penalty_aggregation=cfg.val.penalty_aggregation,
                              device=cfg.device)


def check_connectedness(prev_layer, layer, threshold=0.5, round_upto=1):
    is_connected = False
    if prev_layer is None:
        # On the first layer, we only check if there exists at least one node with a score higher than threshold
        # to check for connectedness as the root node is always selected.
        for node in layer:
            if np.round(node["pred"], round_upto) >= threshold:
                is_connected = True
                node["conn"] = True
    else:
        # Check if we have a high scoring node. If yes, then check if at least one of the parents is also high scoring.
        for node in layer:
            is_node_connected = False
            if np.round(node["pred"], round_upto) >= threshold:
                for prev_one_id in node["op"]:
                    if (np.round(prev_layer[prev_one_id]["pred"], round_upto) >= threshold and
                            "conn" in prev_layer[prev_one_id]):
                        is_connected = True
                        is_node_connected = True
                        node["conn"] = True
                        break

                if not is_node_connected:
                    for prev_zero_id in node["zp"]:
                        if (np.round(prev_layer[prev_zero_id]["pred"], round_upto) >= threshold and
                                "conn" in prev_layer[prev_zero_id]):
                            is_connected = True
                            node["conn"] = True
                        break

    return is_connected


def extend_paths(layer, partial_paths):
    new_partial_paths = []
    for node_idx, node in enumerate(layer):
        for parent_idx in node["op"]:
            for path in partial_paths:
                if path[-1] == parent_idx:
                    new_path = path[:]
                    new_path.append(node_idx)
                    new_partial_paths.append(new_path)

        for parent_idx in node["zp"]:
            for path in partial_paths:
                if path[-1] == parent_idx:
                    new_path = path[:]
                    new_path.append(node_idx)
                    new_partial_paths.append(new_path)

    return new_partial_paths


# def calculate_path_resistance(paths, cl, nl, threshold=0.5, round_upto=1):
def calculate_path_resistance(path, layers, threshold=0.5, round_upto=1):
    resistance = 0
    for node_idx, layer in zip(path[1:], layers[1:]):
        pred_score = layer[node_idx]["pred"]
        _resistance = 0 if np.round(pred_score, round_upto) >= threshold else threshold - pred_score
        resistance += _resistance

    return resistance


def switch_on_node(node, threshold):
    node["prev_pred"] = float(node["pred"])
    node["pred"] = float(threshold + 0.001)
    return node


def generate_resistance_graph(bdd, threshold):
    g = nx.DiGraph()
    root, terminal = "0-0", f"{len(bdd) + 1}-0"

    edges = []
    # From root to penultimate layer
    for lidx, layer in enumerate(bdd):
        for nidx, node in enumerate(layer):
            parent_pre = f"{lidx}"
            node_name = f"{lidx + 1}-{nidx}"
            resistance = max(0, threshold - node["pred"])

            for op in node['op']:
                edges.append((f"{parent_pre}-{op}", node_name, resistance))

            for zp in node['zp']:
                edges.append((f"{parent_pre}-{zp}", node_name, resistance))

    # From penultimate layer to terminal node
    for nidx, _ in enumerate(bdd[-1]):
        edges.append((f"{len(bdd)}-{nidx}", terminal, 0))

    g.add_weighted_edges_from(edges)

    return g, root, terminal


def switch_on_nodes_in_shortest_path(path, bdd, threshold, round_upto):
    assert len(path[1:-1]) == len(bdd)
    for lidx, node_name in enumerate(path[1:-1]):
        _, nidx = list(map(int, node_name.split("-")))
        node = bdd[lidx][nidx]
        if np.round(node["pred"], round_upto) < threshold:
            bdd[lidx][nidx] = switch_on_node(node, threshold)

    return bdd


def generate_min_resistance_mip_model(bdd, threshold=0.5, profile=None):
    root, terminal = "0-0", f"{len(bdd) + 1}-0"
    node_vars, outgoing_arcs = [], {}

    m = gp.Model("Min-resistance Graph")
    # From root to penultimate layer
    for lidx, layer in enumerate(bdd):
        layer_node_vars = []
        for nidx, node in enumerate(layer):
            node_name = f"{lidx}-{nidx}"
            resistance = max(0, threshold - node["pred"])
            node_var = m.addVar(vtype="B", name=node_name, obj=resistance)
            # node_vars.append(node_var)
            layer_node_vars.append(node_var)

            if lidx > 0:
                parent_prefix = f"{lidx - 1}"
                incoming_arcs = []

                for op in node['op']:
                    parent_node_name = f"{parent_prefix}-{op}"
                    arc_name = f"{parent_node_name}-{node_name}-1"
                    if parent_node_name not in outgoing_arcs:
                        outgoing_arcs[parent_node_name] = []

                    arc = m.addVar(vtype="B", name=arc_name, obj=0)
                    outgoing_arcs[parent_node_name].append(arc)
                    incoming_arcs.append(arc)

                for zp in node['zp']:
                    parent_node_name = f"{parent_prefix}-{zp}"
                    arc_name = f"{parent_node_name}-{node_name}-1"
                    if parent_node_name not in outgoing_arcs:
                        outgoing_arcs[parent_node_name] = []

                    arc = m.addVar(vtype="B", name=arc_name, obj=0)
                    outgoing_arcs[parent_node_name].append(arc)
                    incoming_arcs.append(arc)

                # Select node if at least one in coming arc is selected
                m.addConstr(gp.quicksum(incoming_arcs) <= len(incoming_arcs) * node_var)
                # Don't select node if none of the incoming arcs are selected
                m.addConstr(node_var <= gp.quicksum(incoming_arcs))

        node_vars.append(layer_node_vars)

        # For the first (root + 1) and the last (terminal - 1) layers, select at least one node
        if lidx == 0 or lidx == len(bdd) - 1:
            m.addConstr(gp.quicksum(layer_node_vars) >= 1)

        # Select at least on outgoing arc if a node is selected
        if lidx > 0:
            for nidx, node_var in enumerate(node_vars[lidx - 1]):
                parent_node_name = f"{lidx - 1}-{nidx}"
                if parent_node_name not in outgoing_arcs:
                    print("Parent node not found!")
                else:
                    m.addConstr(gp.quicksum(outgoing_arcs[parent_node_name]) >= node_var)

    m._node_vars = node_vars
    return m


def get_mip_solution(mip):
    node_vars = mip._node_vars
    solution = []
    for layer in node_vars:
        _sol = []
        for node in layer:
            _sol.append(np.round(node.x + 0.5))
        solution.append(_sol)

    return solution


def switch_on_nodes_in_mip_solution(bdd, sol):
    for bdd_layer, sol_layer in zip(bdd, sol):
        for bdd_node, is_selected in zip(bdd_layer, sol_layer):
            if is_selected:
                bdd_node["prev_pred"] = bdd_node["pred"]
                bdd_node["pred"] += 0.5

    return bdd


def stitch(bdd, lidx, select_all_upto, heuristic, lookahead, total_time_stitching, threshold=0.5, round_upto=1):
    flag_stitched_layer = False

    # If BDD is disconnected on the first layer, select both nodes.
    time_stitching = time.time()
    actual_lidx = lidx + 1
    if actual_lidx < select_all_upto:
        for node in bdd[lidx]:
            if np.round(node["pred"], round_upto) < threshold:
                node = switch_on_node(node, threshold)
                flag_stitched_layer = True

        time_stitching = time.time() - time_stitching
        return flag_stitched_layer, time_stitching, bdd

    time_stitching = time.time()
    if heuristic == "shortest_path":
        resistance_graph, root, terminal = generate_resistance_graph(bdd, threshold)
        sp = nx.shortest_path(resistance_graph, root, terminal)
        bdd = switch_on_nodes_in_shortest_path(sp, bdd, threshold, round_upto)
        flag_stitched_layer = True
        time_stitching = time.time() - time_stitching
        total_time_stitching += time_stitching
        return flag_stitched_layer, time_stitching, bdd

    elif heuristic == "shortest_path_up_down":
        # Shortest path up down
        # TODO
        pass

    elif heuristic == "mip":
        mip = generate_min_resistance_mip_model(bdd)
        mip.optimize()
        sol = get_mip_solution(mip)
        bdd = switch_on_nodes_in_mip_solution(bdd, sol)
        time_stitching = time.time() - time_stitching
        flag_stitched_layer = True

        return flag_stitched_layer, time_stitching, bdd

    elif heuristic == "min_resistance":
        layers = [bdd[lidx - 1]] if lidx > 0 else None
        if layers is None:
            return flag_stitched_layer, time.time() - time_stitching, bdd

        for i in range(lidx, lidx + lookahead):
            layers.append(bdd[i])

        partial_paths = [[node_idx] for node_idx, node in enumerate(layers[0])
                         if np.round(node["pred"], round_upto) >= threshold and "conn" in node]
        # print("Partial paths len:", len(partial_paths))
        # Extend partial paths
        for i in range(lookahead - 1):
            partial_paths = extend_paths(layers[i + 1], partial_paths)
            # print(np.unique([p[-1] for p in partial_paths]).shape[0])
        paths = extend_paths(layers[-1], partial_paths)
        # print(np.unique([p[-1] for p in paths]).shape[0])

        # Calculate path resistances
        resistances = [calculate_path_resistance(path, layers, threshold=threshold, round_upto=round_upto)
                       for path in paths]

        # Sort paths based on resistance and select the one offering minimum resistance
        resistances, paths = zip(*sorted(zip(resistances, paths), key=lambda x: x[0]))
        k = 1
        for r in resistances[1:]:
            if r > resistances[0]:
                break
            else:
                k += 1
        # print(resistances[:5])
        # Switch on the nodes in the minimum resistance paths
        for path in paths[:k]:
            for node_idx, layer in zip(path[1:], layers[1:]):
                node = layer[node_idx]
                node["conn"] = True
                node = switch_on_node(node, threshold)
            flag_stitched_layer = True

        time_stitching = time.time() - time_stitching
        total_time_stitching += time_stitching

        # for i in range(lidx, lidx + lookahead):
        #     layer = bdd[i]
        #     print(i, len([1 for node in layer if np.round(node["pred"], round_upto) >= threshold]))

        return flag_stitched_layer, total_time_stitching, bdd

    else:
        raise ValueError("Invalid heuristic!")


def get_pareto_states_per_layer(bdd, threshold=0.5, round_upto=1):
    pareto_states_per_layer = []
    for layer in bdd:
        _pareto_states = []
        for node in layer:
            if np.round(node["pred"], round_upto) >= threshold:
                _pareto_states.append(node["s"][0])

        pareto_states_per_layer.append(_pareto_states)

    return pareto_states_per_layer


def get_run_data_from_env(env, order_type, was_disconnected):
    sol = {"x": env.x_sol,
           "z": env.z_sol,
           "ot": order_type}

    data = [was_disconnected,
            env.initial_node_count,
            env.reduced_node_count,
            env.initial_arcs_count,
            env.reduced_arcs_count,
            env.num_comparisons,
            sol,
            env.time_result]

    return data


def get_prediction_stats(bdd, pred_stats_per_layer, threshold=0.5, round_upto=1):
    for lidx, layer in enumerate(bdd):
        labels = np.array([node["l"] for node in layer])
        assert np.max(labels) >= threshold

        preds = np.array([node["pred"] for node in layer])
        score = statscore(preds=preds, labels=labels, threshold=threshold, round_upto=round_upto,
                          is_type="numpy")
        tp, fp, tn, fn = np.sum(score[:, 0]), np.sum(score[:, 1]), np.sum(score[:, 2]), np.sum(score[:, 3])
        pred_stats_per_layer[lidx + 1][1] += tp
        pred_stats_per_layer[lidx + 1][2] += fp
        pred_stats_per_layer[lidx + 1][3] += tn
        pred_stats_per_layer[lidx + 1][4] += fn

    return pred_stats_per_layer


def save_stats_per_layer(cfg, pred_stats_per_layer, mdl_hex):
    df = pd.DataFrame(pred_stats_per_layer,
                      columns=["layer", "tp", "fp", "tn", "fn"])
    name = resource_path / f"predictions/xgb/{cfg.prob.name}/{cfg.prob.size}/{cfg.deploy.split}/{mdl_hex}"
    # if cfg.deploy.stitching_heuristic == "min_resistance":
    #     name /= f"{cfg.deploy.select_all_upto}-mrh{cfg.deploy.lookahead}-spl.csv"
    # elif cfg.deploy.stitching_heuristic:
    #     name /= f"{cfg.deploy.select_all_upto}-sph-spl.csv"
    # else:
    #     raise ValueError("Invalid heuristic!")
    name /= "spl.csv"
    df.to_csv(name, index=False)


def save_bdd_data(cfg, pids, bdd_data, mdl_hex):
    out_path = resource_path / f"predictions/xgb/{cfg.prob.name}/{cfg.prob.size}/{cfg.deploy.split}/{mdl_hex}"
    if cfg.deploy.stitching_heuristic == "min_resistance":
        disconnected_prefix = f"{cfg.deploy.select_all_upto}-mrh{cfg.deploy.lookahead}"
    elif cfg.deploy.stitching_heuristic == "shortest_path":
        disconnected_prefix = f"{cfg.deploy.select_all_upto}-sph"
    else:
        raise ValueError("Invalid heuristic!")

    bdd_stats = []
    bdd_stats_disconnected = []
    for pid, data in zip(pids, bdd_data):
        time_stitching, count_stitching, was_disconnected, inc, rnc, iac, rac, num_comparisons, sol, _time = data

        sol_pred_path = out_path / f"{disconnected_prefix}-sols_pred" \
            if was_disconnected else out_path / "sols_pred"
        sol_pred_path.mkdir(exist_ok=True, parents=True)
        sol_path = sol_pred_path / f"sol_{pid}.json"
        with open(sol_path, "w") as fp:
            json.dump(sol, fp)
        time_path = sol_pred_path / f"time_{pid}.json"
        with open(time_path, "w") as fp:
            json.dump(_time, fp)

        if was_disconnected:
            bdd_stats_disconnected.append([cfg.prob.size,
                                           pid,
                                           cfg.deploy.split,
                                           1,
                                           count_stitching,
                                           time_stitching,
                                           cfg.deploy.stitching_heuristic,
                                           cfg.deploy.lookahead,
                                           cfg.deploy.select_all_upto,
                                           len(sol["x"]),
                                           inc,
                                           rnc,
                                           iac,
                                           rac,
                                           num_comparisons,
                                           _time["compilation"],
                                           _time["reduction"],
                                           _time["pareto"]])
        else:
            bdd_stats.append([cfg.prob.size,
                              pid,
                              cfg.deploy.split,
                              0,
                              0,
                              0,
                              "",
                              "",
                              cfg.deploy.select_all_upto,
                              len(sol["x"]),
                              inc,
                              rnc,
                              iac,
                              rac,
                              num_comparisons,
                              _time["compilation"],
                              _time["reduction"],
                              _time["pareto"]])

    columns = ["size",
               "pid",
               "split",
               "was_disconnected",
               "count_stitching",
               "time_stitching",
               "stitching_heuristic",
               "lookahead",
               "select_all_upto",
               "pred_nnds",
               "pred_inc",
               "pred_rnc",
               "pred_iac",
               "pred_rac",
               "pred_num_comparisons",
               "pred_compile",
               "pred_reduce",
               "pred_pareto"]
    if len(bdd_stats):
        df = pd.DataFrame(bdd_stats, columns=columns)
        df.to_csv(out_path / f"pred_result.csv", index=False)

    if len(bdd_stats_disconnected):
        df = pd.DataFrame(bdd_stats_disconnected, columns=columns)
        df.to_csv(out_path / f"{disconnected_prefix}-pred_result.csv", index=False)


def worker(rank, cfg, mdl_hex):
    env = libbddenvv1.BDDEnv()

    pred_stats_per_layer = np.zeros((cfg.prob.num_vars, 5))
    pred_stats_per_layer[:, 0] = np.arange(pred_stats_per_layer.shape[0])
    pids, bdd_data = [], []
    for pid in range(cfg.deploy.from_pid + rank, cfg.deploy.to_pid, cfg.deploy.num_processes):
        print(pid)
        # Read instance
        inst_data = get_instance_data(cfg.prob.name, cfg.prob.size, cfg.deploy.split, pid)
        order = get_order(cfg.prob.name, cfg.deploy.order_type, inst_data)

        # Load BDD
        bdd_path = resource_path / f"predictions/xgb/{cfg.prob.name}/{cfg.prob.size}/{cfg.deploy.split}/{mdl_hex}/pred_bdd/{pid}.json"
        print(bdd_path)
        if not bdd_path.exists():
            continue
        bdd = json.load(open(bdd_path, "r"))
        bdd = label_bdd(bdd, cfg.deploy.label)
        pred_stats_per_layer = get_prediction_stats(bdd,
                                                    pred_stats_per_layer,
                                                    threshold=cfg.deploy.threshold,
                                                    round_upto=cfg.deploy.round_upto)

        # Check connectedness of predicted Pareto BDD and perform stitching if necessary
        was_disconnected = False
        total_time_stitching = 0
        count_stitching = 0
        flag_stitched_layer = False
        for lidx, layer in enumerate(bdd):
            prev_layer = bdd[lidx - 1] if lidx > 0 else None
            # if prev_layer is not None:
            #     print("For ", lidx - 1, len([1 for node in prev_layer if np.round(node["pred"], 1) >= 0.5]))
            is_connected = check_connectedness(prev_layer,
                                               layer,
                                               threshold=cfg.deploy.threshold,
                                               round_upto=cfg.deploy.round_upto)

            if lidx == 0:
                for node in bdd[0]:
                    if "conn" in node:
                        print("Conn in node!")
            if not is_connected:
                print(f"Disconnected {pid}, layer: ", lidx)
                was_disconnected = True
                count_stitching += 1
                out = stitch(bdd,
                             lidx,
                             cfg.deploy.select_all_upto,
                             cfg.deploy.stitching_heuristic,
                             cfg.deploy.lookahead,
                             total_time_stitching,
                             threshold=cfg.deploy.threshold,
                             round_upto=cfg.deploy.round_upto)
                flag_stitched_layer, total_time_stitching, bdd = out
                if flag_stitched_layer is False:
                    break

        if was_disconnected and flag_stitched_layer is False:
            continue

        if cfg.deploy.process_connected is True or was_disconnected:
            # Compute Pareto frontier on predicted Pareto BDD
            env.set_knapsack_inst(cfg.prob.num_vars,
                                  cfg.prob.num_objs,
                                  inst_data['value'],
                                  inst_data['weight'],
                                  inst_data['capacity'])
            env.initialize_run(cfg.bin.problem_type,
                               cfg.bin.preprocess,
                               cfg.bin.bdd_type,
                               cfg.bin.maxwidth,
                               order)
            pareto_states = get_pareto_states_per_layer(bdd,
                                                        threshold=cfg.deploy.threshold,
                                                        round_upto=cfg.deploy.round_upto)
            env.compute_pareto_frontier_with_pruning(pareto_states)

            # Extract run info
            _data = get_run_data_from_env(env, cfg.deploy.order_type, was_disconnected)
            _data1 = [total_time_stitching, count_stitching]
            _data1.extend(_data)

            pids.append(pid)
            bdd_data.append(_data1)
            print(f'Processed: {pid}, was_disconnected: {_data[0]}, n_sols: {len(_data[-2]["x"])}')

    return pids, bdd_data, pred_stats_per_layer


@hydra.main(version_base="1.2", config_path="./configs", config_name="deploy_xgb.yaml")
def main(cfg):
    mdl_name = call_get_model_name(cfg)
    # Convert to hex
    h = hashlib.blake2s(digest_size=32)
    h.update(mdl_name.encode("utf-8"))
    mdl_hex = h.hexdigest()
    print(mdl_hex)
    # Deploy model
    pool = mp.Pool(processes=cfg.deploy.num_processes)
    results = []
    for rank in range(cfg.deploy.num_processes):
        results.append(pool.apply_async(worker, args=(rank, cfg, mdl_hex)))

    # results = [worker(0, cfg, mdl_hex)]

    # Fetch results
    results = [r.get() for r in results]
    pids, bdd_data = [], []
    pred_stats_per_layer = np.zeros((cfg.prob.num_vars, 5))
    pred_stats_per_layer[:, 0] = np.arange(pred_stats_per_layer.shape[0])
    for r in results:
        pids.extend(r[0])
        bdd_data.extend(r[1])
        pred_stats_per_layer[:, 1:] += r[2][:, 1:]

    # Save results
    save_bdd_data(cfg, pids, bdd_data, mdl_hex)
    save_stats_per_layer(cfg, pred_stats_per_layer, mdl_hex)


if __name__ == '__main__':
    main()
